const STATUS_LABELS = {
  pending: "待定",
  accepted: "接受",
  rejected: "拒绝",
};
const AI_STATUS_LABELS = {
  pending: "AI 待定",
  accepted: "AI 接受",
  rejected: "AI 拒绝",
  unknown: "AI 未标注",
};
const AI_WORKER_LABELS = {
  disabled: "已关闭",
  idle: "空闲",
  running: "运行中",
  error: "异常",
};
const REVIEW_SESSION_STORAGE_KEY = "reviewSessionKey";
const REVIEW_PREFER_AI_STORAGE_KEY = "reviewPreferAi";
let fallbackReviewSessionKey = null;

function getCurrentPage() {
  return document.body?.dataset.page || "home";
}

function getReviewSessionKey() {
  try {
    let sessionKey = window.sessionStorage.getItem(REVIEW_SESSION_STORAGE_KEY);
    if (!sessionKey) {
      sessionKey =
        window.crypto?.randomUUID?.() ||
        `review-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      window.sessionStorage.setItem(REVIEW_SESSION_STORAGE_KEY, sessionKey);
    }
    return sessionKey;
  } catch {
    if (!fallbackReviewSessionKey) {
      fallbackReviewSessionKey = `review-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    }
    return fallbackReviewSessionKey;
  }
}

const state = {
  activeView: getCurrentPage(),
  review: {
    timeline: [],
    pointer: -1,
    loading: false,
    canGoBack: false,
    mode: "random",
    preferAi: getStoredReviewPreferAi(),
    markedCount: 0,
  },
  ai: {
    overview: null,
    loading: false,
    pollTimer: null,
  },
  manage: {
    page: 1,
    pageSize: 30,
    status: "all",
    aiStatus: "all",
    query: "",
    minWeight: "",
    maxWeight: "",
    totalPages: 1,
    currentItems: [],
    selectedIds: new Set(),
  },
  edit: {
    entryId: null,
    forceClearLabeledAt: false,
    originalEntry: null,
  },
};

const els = {};

document.addEventListener("DOMContentLoaded", async () => {
  state.activeView = getCurrentPage();
  cacheElements();
  bindEvents();
  initTooltips();
  updateSelectedCount();
  await refreshStats();

  if (els.reviewCard) {
    bindReviewWordAutoFit();
    await ensureReviewEntry(true);
  }

  if (els.manageFilterForm) {
    state.manage.query = els.manageQuery?.value.trim() || "";
    state.manage.status = els.manageStatus?.value || "all";
    state.manage.aiStatus = els.manageAiStatus?.value || "all";
    state.manage.minWeight = els.manageMinWeight?.value.trim() || "";
    state.manage.maxWeight = els.manageMaxWeight?.value.trim() || "";
    state.manage.pageSize = Number(els.managePageSize?.value || state.manage.pageSize);
    await loadAiOverview();
    await loadManageEntries();
  }

  if (els.exportForm) {
    await updateExportCount();
  }

  if (els.aiEnabledToggle) {
    startAiOverviewPolling();
  }
});

function initTooltips() {
  document.body.classList.add("js-tooltips");
  const tooltip = document.createElement("div");
  tooltip.className = "floating-tooltip";
  tooltip.hidden = true;
  tooltip.setAttribute("role", "tooltip");
  document.body.appendChild(tooltip);

  let activeTrigger = null;

  const show = (trigger) => {
    const text = trigger.dataset.tip || "";
    if (!text) return;
    activeTrigger = trigger;
    tooltip.textContent = text;
    tooltip.hidden = false;
    positionTooltip(trigger, tooltip);
  };

  const hide = (trigger) => {
    if (activeTrigger !== trigger) return;
    activeTrigger = null;
    tooltip.hidden = true;
  };

  document.addEventListener("mouseover", (event) => {
    const trigger = event.target.closest(".has-tip[data-tip], [data-tip]");
    if (!trigger || !document.body.contains(trigger) || trigger === activeTrigger) return;
    show(trigger);
  });

  document.addEventListener("mouseout", (event) => {
    const trigger = event.target.closest(".has-tip[data-tip], [data-tip]");
    if (!trigger || trigger.contains(event.relatedTarget)) return;
    hide(trigger);
  });

  document.addEventListener("focusin", (event) => {
    const trigger = event.target.closest(".has-tip[data-tip], [data-tip]");
    if (trigger) {
      show(trigger);
    }
  });

  document.addEventListener("focusout", (event) => {
    const trigger = event.target.closest(".has-tip[data-tip], [data-tip]");
    if (trigger) {
      hide(trigger);
    }
  });

  ["scroll", "resize"].forEach((eventName) => {
    window.addEventListener(
      eventName,
      () => {
        if (!activeTrigger || tooltip.hidden) return;
        positionTooltip(activeTrigger, tooltip);
      },
      { passive: true },
    );
  });
}

function positionTooltip(trigger, tooltip) {
  const margin = 12;
  const rect = trigger.getBoundingClientRect();
  tooltip.style.maxWidth = `${Math.max(120, Math.min(360, window.innerWidth - margin * 2))}px`;
  tooltip.classList.remove("below");

  const tooltipRect = tooltip.getBoundingClientRect();
  const maxLeft = window.innerWidth - tooltipRect.width - margin;
  const left = Math.max(margin, Math.min(rect.left, maxLeft));
  let top = rect.top - tooltipRect.height - margin;

  if (top < margin) {
    top = rect.bottom + margin;
    tooltip.classList.add("below");
  }
  const maxTop = window.innerHeight - tooltipRect.height - margin;
  top = Math.max(margin, Math.min(top, maxTop));

  const anchorCenter = rect.left + rect.width / 2;
  const arrowLeft = Math.max(18, Math.min(anchorCenter - left, tooltipRect.width - 18));
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${top}px`;
  tooltip.style.setProperty("--tip-arrow-left", `${arrowLeft}px`);
}

function getStoredReviewPreferAi() {
  try {
    return window.localStorage.getItem(REVIEW_PREFER_AI_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function storeReviewPreferAi(preferAi) {
  try {
    window.localStorage.setItem(REVIEW_PREFER_AI_STORAGE_KEY, preferAi ? "1" : "0");
  } catch {
    // Ignore storage failures and keep using in-memory mode.
  }
}

function cacheElements() {
  els.statTotal = document.getElementById("statTotal");
  els.statPending = document.getElementById("statPending");
  els.statAccepted = document.getElementById("statAccepted");
  els.statRejected = document.getElementById("statRejected");

  els.reviewCard = document.getElementById("reviewCard");
  els.reviewStatus = document.getElementById("reviewStatus");
  els.reviewSessionCount = document.getElementById("reviewSessionCount");
  els.reviewWord = document.getElementById("reviewWord");
  els.reviewPinyin = document.getElementById("reviewPinyin");
  els.reviewWeight = document.getElementById("reviewWeight");
  els.reviewImportedAt = document.getElementById("reviewImportedAt");
  els.reviewLabeledAt = document.getElementById("reviewLabeledAt");
  els.reviewAiStatus = document.getElementById("reviewAiStatus");
  els.reviewAiScore = document.getElementById("reviewAiScore");
  els.reviewAgreeAiButton = document.getElementById("reviewAgreeAiButton");
  els.reviewHistoryMenu = document.getElementById("reviewHistoryMenu");
  els.reviewHistoryDropdown = document.getElementById("reviewHistoryDropdown");
  els.reviewPinyinForm = document.getElementById("reviewPinyinForm");
  els.reviewPinyinInput = document.getElementById("reviewPinyinInput");
  els.reviewAutoPinyinButton = document.getElementById("reviewAutoPinyinButton");
  els.reviewSavePinyinButton = document.getElementById("reviewSavePinyinButton");
  els.reviewPreferAiToggle = document.getElementById("reviewPreferAiToggle");

  els.importForm = document.getElementById("importForm");
  els.importFile = document.getElementById("importFile");
  els.importText = document.getElementById("importText");
  els.importOverwritePinyin = document.getElementById("importOverwritePinyin");
  els.importIgnorePinyin = document.getElementById("importIgnorePinyin");
  els.importOverwriteWeight = document.getElementById("importOverwriteWeight");
  els.importMarkAccepted = document.getElementById("importMarkAccepted");
  els.importSkipNewEntries = document.getElementById("importSkipNewEntries");
  els.importBackupBeforeImport = document.getElementById("importBackupBeforeImport");
  els.importMessage = document.getElementById("importMessage");

  els.exportForm = document.getElementById("exportForm");
  els.exportName = document.getElementById("exportName");
  els.includeWeight = document.getElementById("includeWeight");
  els.includeAiAssist = document.getElementById("includeAiAssist");
  els.omitYamlHeader = document.getElementById("omitYamlHeader");
  els.includeMixedWords = document.getElementById("includeMixedWords");
  els.mixedPinyinScheme = document.getElementById("mixedPinyinScheme");
  els.exportCountNote = document.getElementById("exportCountNote");
  els.importLoadingOverlay = document.getElementById("importLoadingOverlay");
  els.importLoadingText = document.getElementById("importLoadingText");

  els.manageFilterForm = document.getElementById("manageFilterForm");
  els.manageQuery = document.getElementById("manageQuery");
  els.manageStatus = document.getElementById("manageStatus");
  els.manageAiStatus = document.getElementById("manageAiStatus");
  els.manageMinWeight = document.getElementById("manageMinWeight");
  els.manageMaxWeight = document.getElementById("manageMaxWeight");
  els.managePageSize = document.getElementById("managePageSize");
  els.entryList = document.getElementById("entryList");
  els.pageInfo = document.getElementById("pageInfo");
  els.pagePrev = document.getElementById("pagePrev");
  els.pageNext = document.getElementById("pageNext");
  els.pageJumpForm = document.getElementById("pageJumpForm");
  els.pageJumpInput = document.getElementById("pageJumpInput");
  els.selectedCount = document.getElementById("selectedCount");
  els.selectPageButton = document.getElementById("selectPageButton");
  els.clearSelectionButton = document.getElementById("clearSelectionButton");
  els.bulkAcceptButton = document.getElementById("bulkAcceptButton");
  els.bulkPendingButton = document.getElementById("bulkPendingButton");
  els.bulkRejectButton = document.getElementById("bulkRejectButton");
  els.bulkLockPinyinButton = document.getElementById("bulkLockPinyinButton");
  els.bulkUnlockPinyinButton = document.getElementById("bulkUnlockPinyinButton");
  els.openBulkEditButton = document.getElementById("openBulkEditButton");
  els.aiEnabledToggle = document.getElementById("aiEnabledToggle");
  els.aiPanelNote = document.getElementById("aiPanelNote");
  els.aiTrainingSummary = document.getElementById("aiTrainingSummary");
  els.aiQueueSummary = document.getElementById("aiQueueSummary");
  els.aiWorkerStatus = document.getElementById("aiWorkerStatus");
  els.aiModelSummary = document.getElementById("aiModelSummary");
  els.aiProgressPanel = document.getElementById("aiProgressPanel");
  els.aiProgressTitle = document.getElementById("aiProgressTitle");
  els.aiProgressMeta = document.getElementById("aiProgressMeta");
  els.aiProgressDone = document.getElementById("aiProgressDone");
  els.aiProgressUnlabeled = document.getElementById("aiProgressUnlabeled");
  els.aiProgressOutdated = document.getElementById("aiProgressOutdated");
  els.aiLastError = document.getElementById("aiLastError");
  els.recomputeTonelessPinyinButton = document.getElementById("recomputeTonelessPinyinButton");
  els.capRejectedWeightsButton = document.getElementById("capRejectedWeightsButton");
  els.reprocessOutdatedAiButton = document.getElementById("reprocessOutdatedAiButton");
  els.maintenanceResult = document.getElementById("maintenanceResult");

  els.entryEditDialog = document.getElementById("entryEditDialog");
  els.entryEditForm = document.getElementById("entryEditForm");
  els.entryEditCloseButton = document.getElementById("entryEditCloseButton");
  els.editEntryId = document.getElementById("editEntryId");
  els.editPhrase = document.getElementById("editPhrase");
  els.editPinyin = document.getElementById("editPinyin");
  els.editWeight = document.getElementById("editWeight");
  els.editStatus = document.getElementById("editStatus");
  els.editImportedAt = document.getElementById("editImportedAt");
  els.editLabeledAt = document.getElementById("editLabeledAt");
  els.editAutoPinyinButton = document.getElementById("editAutoPinyinButton");
  els.editClearLabeledAtButton = document.getElementById("editClearLabeledAtButton");

  els.bulkEditDialog = document.getElementById("bulkEditDialog");
  els.bulkEditForm = document.getElementById("bulkEditForm");
  els.bulkEditCloseButton = document.getElementById("bulkEditCloseButton");
  els.bulkTargetSummary = document.getElementById("bulkTargetSummary");
  els.bulkApplyStatus = document.getElementById("bulkApplyStatus");
  els.bulkStatus = document.getElementById("bulkStatus");
  els.bulkApplyWeight = document.getElementById("bulkApplyWeight");
  els.bulkWeight = document.getElementById("bulkWeight");
  els.bulkApplyImportedAt = document.getElementById("bulkApplyImportedAt");
  els.bulkImportedAt = document.getElementById("bulkImportedAt");
  els.bulkApplyPinyin = document.getElementById("bulkApplyPinyin");
  els.bulkPinyin = document.getElementById("bulkPinyin");
  els.bulkRegeneratePinyin = document.getElementById("bulkRegeneratePinyin");
  els.bulkApplyLabeledAt = document.getElementById("bulkApplyLabeledAt");
  els.bulkLabeledAt = document.getElementById("bulkLabeledAt");
  els.bulkClearLabeledAt = document.getElementById("bulkClearLabeledAt");

  els.toast = document.getElementById("toast");
}

function bindEvents() {
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });

  if (els.reviewCard) {
    document.querySelectorAll(".decision-button[data-status]").forEach((button) => {
      button.addEventListener("click", () => labelCurrent(button.dataset.status));
    });

    els.reviewPinyinForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      await saveCurrentReviewPinyin(false);
    });
    els.reviewAutoPinyinButton.addEventListener("click", async () => {
      await fillReviewPinyinFromPhrase();
    });
    els.reviewAgreeAiButton?.addEventListener("click", async () => {
      await agreeWithAiSuggestion();
    });
    if (els.reviewPreferAiToggle) {
      els.reviewPreferAiToggle.checked = state.review.preferAi;
      els.reviewPreferAiToggle.addEventListener("change", () => {
        state.review.preferAi = els.reviewPreferAiToggle.checked;
        storeReviewPreferAi(state.review.preferAi);
        state.review.timeline = [];
        state.review.pointer = -1;
        state.review.canGoBack = false;
        void advanceReview("next", false);
      });
    }
    els.reviewHistoryDropdown?.addEventListener("click", (event) => {
      const option = event.target.closest("[data-history-target]");
      if (!option) return;
      jumpToReviewHistory(option.dataset.historyTarget);
    });
  }

  document.addEventListener("click", (event) => {
    document.querySelectorAll("details[open]").forEach((detail) => {
      if (!detail.contains(event.target)) {
        detail.removeAttribute("open");
      }
    });
  });

  document.addEventListener("keydown", async (event) => {
    if (state.activeView !== "review") return;

    if ((event.ctrlKey || event.metaKey) && (event.key === "s" || event.key === "S")) {
      event.preventDefault();
      await saveCurrentReviewPinyin(false);
      return;
    }

    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      await saveCurrentReviewPinyin(false);
      return;
    }

    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;

    const key = event.key;
    if (key === " " || key === "Spacebar" || event.code === "Space") {
      if (canAgreeWithAiSuggestion()) {
        event.preventDefault();
        if (event.repeat) return;
        await agreeWithAiSuggestion();
      }
    } else if (key === "ArrowUp" || ["w", "W", "i", "I", "0"].includes(key)) {
      event.preventDefault();
      goBackReviewHistory();
    } else if (key === "ArrowLeft" || ["j", "J", "1", "a", "A"].includes(key)) {
      event.preventDefault();
      await labelCurrent("accepted");
    } else if (key === "ArrowDown" || ["k", "K", "2", "s", "S"].includes(key)) {
      event.preventDefault();
      await labelCurrent("pending");
    } else if (key === "ArrowRight" || ["l", "L", "3", "d", "D"].includes(key)) {
      event.preventDefault();
      await labelCurrent("rejected");
    }
  });

  if (els.importForm) {
    syncImportPinyinOptions();
    els.importIgnorePinyin?.addEventListener("change", syncImportPinyinOptions);
    els.importOverwritePinyin?.addEventListener("change", () => {
      if (els.importOverwritePinyin.checked && els.importIgnorePinyin) {
        els.importIgnorePinyin.checked = false;
      }
      syncImportPinyinOptions();
    });
    els.importText?.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      event.preventDefault();
      insertTextAtSelection(els.importText, "\t");
    });

    els.importForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const file = els.importFile.files?.[0] || null;
      const text = cleanImportText(els.importText.value).trim();
      const overwritePinyin = !!els.importOverwritePinyin?.checked;
      const ignorePinyin = !!els.importIgnorePinyin?.checked;
      const overwriteWeight = els.importOverwriteWeight?.checked !== false;
      const markAccepted = !!els.importMarkAccepted?.checked;
      const skipNewEntries = !!els.importSkipNewEntries?.checked;
      const backupBeforeImport = !!els.importBackupBeforeImport?.checked;

      if (!file && !text) {
        showMessage("请先选择文件或粘贴导入内容。");
        return;
      }

      try {
        setImportBusy(true, file ? `正在导入文件：${file.name}` : "正在处理粘贴的词库内容");
        const importParams = new URLSearchParams({
          overwrite_pinyin: overwritePinyin ? "1" : "0",
          ignore_pinyin: ignorePinyin ? "1" : "0",
          overwrite_weight: overwriteWeight ? "1" : "0",
          mark_accepted: markAccepted ? "1" : "0",
          skip_new_entries: skipNewEntries ? "1" : "0",
          backup_before_import: backupBeforeImport ? "1" : "0",
        });
        const payload = file
          ? await postRawText(`/api/import-file?${importParams.toString()}`, file)
          : await postJSON("/api/import", {
              text,
              overwrite_pinyin: overwritePinyin,
              ignore_pinyin: ignorePinyin,
              overwrite_weight: overwriteWeight,
              mark_accepted: markAccepted,
              skip_new_entries: skipNewEntries,
              backup_before_import: backupBeforeImport,
            });
        const result = payload.result;
        const updateSummary = result.updated
          ? `，更新 ${result.updated} 条重复词条（拼音 ${result.updated_pinyin}，词频升高/定义 ${result.updated_weight}）`
          : "";
        const acceptedSummary = result.accepted_marked
          ? `，本次标注接受 ${result.accepted_marked} 条`
          : "";
        const skippedNewSummary = result.skipped_new
          ? `，跳过 ${result.skipped_new} 条新词条`
          : "";
        const backupSummary = result.backup_path ? `已备份到 ${result.backup_path}。` : "";
        const summary = `词库包含 ${result.parsed} 条，其中 ${result.inserted} 条新词条${skippedNewSummary}${updateSummary}${acceptedSummary}，${result.accepted_existing} 条已被标注为接受，${result.rejected_existing} 条已被标注为拒绝。${backupSummary}`;
        showMessage(summary);
        els.importText.value = "";
        els.importFile.value = "";
        await refreshStats(payload.stats);
        await updateExportCount();
        showToast(summary);
      } catch (error) {
        showMessage(error.message, true);
      } finally {
        setImportBusy(false);
      }
    });

    els.exportForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const statuses = [...document.querySelectorAll('input[name="exportStatus"]:checked')].map(
        (input) => input.value
      );
      if (!statuses.length) {
        showToast("至少选择一个导出状态。", true);
        return;
      }

      const params = new URLSearchParams();
      params.set("statuses", statuses.join(","));
      params.set("include_weight", els.includeWeight.checked ? "1" : "0");
      params.set("include_ai_assist", els.includeAiAssist.checked ? "1" : "0");
      params.set("omit_yaml_header", els.omitYamlHeader?.checked ? "1" : "0");
      params.set("include_mixed", els.includeMixedWords?.checked ? "1" : "0");
      params.set("mixed_scheme", els.mixedPinyinScheme?.value || "full_pinyin");
      params.set("name", els.exportName.value.trim() || "rime_word_marker_export");
      window.location.href = `/api/export?${params.toString()}`;
    });

    syncMixedExportOptions();
    [
      els.includeAiAssist,
      els.includeMixedWords,
      els.mixedPinyinScheme,
      ...document.querySelectorAll('input[name="exportStatus"]'),
    ].filter(Boolean).forEach((input) => {
      input.addEventListener("change", () => {
        syncMixedExportOptions();
        void updateExportCount();
      });
    });
  }

  if (els.manageFilterForm) {
    els.manageFilterForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      state.manage.page = 1;
      state.manage.query = els.manageQuery.value.trim();
      state.manage.status = els.manageStatus.value;
      state.manage.aiStatus = els.manageAiStatus.value;
      state.manage.minWeight = els.manageMinWeight?.value.trim() || "";
      state.manage.maxWeight = els.manageMaxWeight?.value.trim() || "";
      state.manage.pageSize = Number(els.managePageSize.value);
      await loadManageEntries();
    });

    els.pagePrev.addEventListener("click", async () => {
      if (state.manage.page <= 1) return;
      state.manage.page -= 1;
      await loadManageEntries();
    });

    els.pageNext.addEventListener("click", async () => {
      if (state.manage.page >= state.manage.totalPages) return;
      state.manage.page += 1;
      await loadManageEntries();
    });

    els.pageJumpForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      await jumpToManagePage();
    });

    els.selectPageButton.addEventListener("click", () => {
      state.manage.currentItems.forEach((item) => state.manage.selectedIds.add(item.id));
      updateSelectedCount();
      renderEntryList(state.manage.currentItems);
    });

    els.clearSelectionButton.addEventListener("click", () => {
      state.manage.selectedIds.clear();
      updateSelectedCount();
      renderEntryList(state.manage.currentItems);
    });

    els.bulkAcceptButton.addEventListener("click", async () => {
      await applyBulkStatus("accepted");
    });
    els.bulkPendingButton.addEventListener("click", async () => {
      await applyBulkStatus("pending");
    });
    els.bulkRejectButton.addEventListener("click", async () => {
      await applyBulkStatus("rejected");
    });
    els.bulkLockPinyinButton?.addEventListener("click", async () => {
      await applyBulkPinyinLock(true);
    });
    els.bulkUnlockPinyinButton?.addEventListener("click", async () => {
      await applyBulkPinyinLock(false);
    });
    els.openBulkEditButton.addEventListener("click", openBulkEditDialog);
    els.aiEnabledToggle?.addEventListener("change", async () => {
      await toggleAiEnabled(els.aiEnabledToggle.checked);
    });
    els.recomputeTonelessPinyinButton?.addEventListener("click", async () => {
      await recomputeTonelessPinyin();
    });
    els.capRejectedWeightsButton?.addEventListener("click", async () => {
      await capRejectedWeights();
    });
    els.reprocessOutdatedAiButton?.addEventListener("click", async () => {
      await reprocessOutdatedAi();
    });

    els.entryList.addEventListener("click", async (event) => {
      const editButton = event.target.closest("[data-edit-entry-id]");
      if (editButton) {
        await openEditDialog(Number(editButton.dataset.editEntryId));
        return;
      }

      const pinyinLockButton = event.target.closest("[data-toggle-pinyin-lock-id]");
      if (pinyinLockButton) {
        await toggleEntryPinyinLock(Number(pinyinLockButton.dataset.togglePinyinLockId));
        return;
      }

      const button = event.target.closest("[data-entry-id][data-status]");
      if (!button) return;

      try {
        const payload = await postJSON(`/api/entries/${button.dataset.entryId}/status`, {
          status: button.dataset.status,
        });
        syncEntryAcrossViews(payload.entry);
        await refreshStats(payload.stats);
        void loadAiOverview(false);
        await loadManageEntries();
        showToast(`词条已标记为${STATUS_LABELS[button.dataset.status]}。`);
      } catch (error) {
        showToast(error.message, true);
      }
    });

    els.entryList.addEventListener("change", (event) => {
      const checkbox = event.target.closest("[data-select-entry-id]");
      if (!checkbox) return;

      const entryId = Number(checkbox.dataset.selectEntryId);
      if (checkbox.checked) {
        state.manage.selectedIds.add(entryId);
      } else {
        state.manage.selectedIds.delete(entryId);
      }

      updateSelectedCount();
      renderEntryList(state.manage.currentItems);
    });

    bindDialogEvents(els.entryEditDialog, closeEditDialog);
    els.entryEditCloseButton.addEventListener("click", closeEditDialog);
    els.entryEditForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      await saveEditForm();
    });
    els.editAutoPinyinButton.addEventListener("click", async () => {
      await fillEditPinyinFromPhrase();
    });
    els.editClearLabeledAtButton.addEventListener("click", () => {
      state.edit.forceClearLabeledAt = true;
      els.editLabeledAt.value = "";
    });
    els.editLabeledAt.addEventListener("input", () => {
      state.edit.forceClearLabeledAt = false;
    });

    bindDialogEvents(els.bulkEditDialog, closeBulkEditDialog);
    els.bulkEditCloseButton.addEventListener("click", closeBulkEditDialog);
    els.bulkEditForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      await saveBulkEditForm();
    });
    [
      els.bulkApplyStatus,
      els.bulkApplyWeight,
      els.bulkApplyImportedAt,
      els.bulkApplyPinyin,
      els.bulkApplyLabeledAt,
      els.bulkRegeneratePinyin,
      els.bulkClearLabeledAt,
    ].forEach((checkbox) => {
      checkbox.addEventListener("change", updateBulkFieldStates);
    });
  }
}

function syncImportPinyinOptions() {
  if (!els.importIgnorePinyin || !els.importOverwritePinyin) return;
  if (els.importIgnorePinyin.checked) {
    els.importOverwritePinyin.checked = false;
    els.importOverwritePinyin.disabled = true;
  } else {
    els.importOverwritePinyin.disabled = false;
  }
}

function cleanImportText(text) {
  return String(text || "").replace(/\u200c/g, "");
}

function insertTextAtSelection(input, text) {
  if (!input) return;
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? input.value.length;
  const before = input.value.slice(0, start);
  const after = input.value.slice(end);
  input.value = `${before}${text}${after}`;
  const nextCursor = start + text.length;
  input.selectionStart = nextCursor;
  input.selectionEnd = nextCursor;
}

function bindDialogEvents(dialog, onClose) {
  if (!dialog) return;
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    onClose();
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      onClose();
    }
  });
}

function switchView(viewName) {
  if (!viewName) return;
  state.activeView = viewName;
  document.querySelectorAll(".nav-chip").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === viewName);
  });

  document.querySelectorAll(".view").forEach((section) => {
    section.classList.toggle("active", section.id === `view-${viewName}`);
  });

  if (viewName === "review") {
    ensureReviewEntry();
  } else if (viewName === "manage") {
    loadManageEntries();
  }
}

async function refreshStats(existingStats = null) {
  if (!els.statTotal) return;
  const stats = existingStats || (await fetchJSON("/api/stats"));
  els.statTotal.textContent = stats.total ?? 0;
  els.statPending.textContent = stats.pending ?? 0;
  els.statAccepted.textContent = stats.accepted ?? 0;
  els.statRejected.textContent = stats.rejected ?? 0;
}

function startAiOverviewPolling() {
  if (!els.aiEnabledToggle || state.ai.pollTimer) return;
  state.ai.pollTimer = window.setInterval(() => {
    if (document.visibilityState === "hidden") return;
    void loadAiOverview(false);
  }, 8000);
}

async function loadAiOverview(showErrors = true) {
  if (!els.aiEnabledToggle) return;
  try {
    const overview = await fetchJSON("/api/ai/overview");
    applyAiOverview(overview);
  } catch (error) {
    if (showErrors) {
      showToast(error.message, true);
    }
  }
}

function applyAiOverview(overview) {
  state.ai.overview = overview;
  if (!els.aiEnabledToggle) return;
  els.aiEnabledToggle.checked = !!overview.enabled;
  els.aiEnabledToggle.disabled = !overview.configured && !overview.enabled;
  els.aiTrainingSummary.textContent =
    `接受 ${overview.training.accepted} / 拒绝 ${overview.training.rejected}`;
  const outdatedText = overview.queue.outdated ? `，旧 prompt ${overview.queue.outdated}` : "";
  const pendingQueueText = overview.queue.reprocess_outdated
    ? `待跑 ${overview.queue.remaining ?? overview.queue.unlabeled}`
    : `待标注 ${overview.queue.unlabeled}`;
  els.aiQueueSummary.textContent =
    `${pendingQueueText}${outdatedText}，待定 ${overview.queue.ai_pending}，接受 ${overview.queue.ai_accepted}，拒绝 ${overview.queue.ai_rejected}`;
  els.aiWorkerStatus.textContent = AI_WORKER_LABELS[overview.worker_status] || overview.worker_status;
  els.aiModelSummary.textContent = overview.configured
    ? `${overview.model || "已配置"} · prompt ${overview.prompt_version || "-"}`
    : "未配置";
  renderAiProgress(overview);
  if (overview.enabled) {
    els.aiPanelNote.textContent = overview.queue.reprocess_outdated
      ? "后台会持续为未 AI 标注和旧 prompt 待更新的待定词条生成辅助建议。"
      : "后台会持续为尚未 AI 标注的待定词条生成辅助建议；旧 prompt 词条需在全局维护中手动触发重算。";
  } else if (!overview.configured) {
    els.aiPanelNote.textContent = "请先在配置文件里补全 AI endpoint、model 等参数。";
  } else {
    els.aiPanelNote.textContent = overview.requirement_message;
  }

  if (overview.last_error) {
    els.aiLastError.hidden = false;
    els.aiLastError.textContent = `最近提示：${overview.last_error}`;
  } else {
    els.aiLastError.hidden = true;
    els.aiLastError.textContent = "";
  }
}

function renderAiProgress(overview) {
  if (!els.aiProgressPanel) return;
  const progress = overview.progress;
  if (!overview.enabled || !progress) {
    els.aiProgressPanel.hidden = true;
    return;
  }

  els.aiProgressPanel.hidden = false;
  const total = Math.max(0, Number(progress.total || 0));
  const current = Math.max(0, Number(progress.current || 0));
  const unlabeled = Math.max(0, Number(progress.unlabeled || 0));
  const outdated = Math.max(0, Number(progress.outdated || 0));
  const donePct = total ? clampPercent((current / total) * 100) : 0;
  const unlabeledPct = total ? clampPercent((unlabeled / total) * 100) : 0;
  const outdatedPct = total ? clampPercent((outdated / total) * 100) : 0;
  const coveredPct = total ? Math.round((current / total) * 100) : 100;
  els.aiProgressDone.style.width = `${donePct}%`;
  els.aiProgressUnlabeled.style.width = `${unlabeledPct}%`;
  els.aiProgressOutdated.style.width = `${outdatedPct}%`;
  els.aiProgressTitle.textContent = `当前覆盖 ${coveredPct}%`;

  const rateText =
    progress.rate_per_minute == null
      ? "速度收集中"
      : `速度 ${Number(progress.rate_per_minute).toFixed(1)} 条/分钟`;
  const etaText =
    progress.eta_seconds == null
      ? "预计 -"
      : `预计 ${formatDuration(progress.eta_seconds)}`;
  els.aiProgressMeta.textContent =
    `未标注 ${unlabeled} · 旧 prompt ${outdated} · ${rateText} · ${etaText}`;
}

function clampPercent(value) {
  return Math.min(100, Math.max(0, Number.isFinite(value) ? value : 0));
}

function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Number(totalSeconds) || 0);
  if (seconds < 60) return `${Math.ceil(seconds)} 秒`;
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours < 24) {
    return remainingMinutes ? `${hours} 小时 ${remainingMinutes} 分钟` : `${hours} 小时`;
  }
  const days = Math.floor(hours / 24);
  const remainingHours = hours % 24;
  return remainingHours ? `${days} 天 ${remainingHours} 小时` : `${days} 天`;
}

async function toggleAiEnabled(enabled) {
  if (!els.aiEnabledToggle) return;
  if (state.ai.loading) return;
  state.ai.loading = true;
  els.aiEnabledToggle.disabled = true;
  try {
    const payload = await postJSON("/api/ai/toggle", { enabled });
    applyAiOverview(payload.overview);
    if (payload.message) {
      showToast(payload.message, !payload.overview.enabled && enabled);
    } else {
      showToast(payload.overview.enabled ? "已开启自动 AI 标注。" : "已关闭自动 AI 标注。");
    }
  } catch (error) {
    els.aiEnabledToggle.checked = !enabled;
    showToast(error.message, true);
  } finally {
    state.ai.loading = false;
    if (state.ai.overview) {
      els.aiEnabledToggle.disabled = !state.ai.overview.configured && !state.ai.overview.enabled;
    } else {
      els.aiEnabledToggle.disabled = false;
    }
  }
}

async function recomputeTonelessPinyin() {
  if (!els.recomputeTonelessPinyinButton) return;
  const confirmed = window.confirm(
    "将用内置拼音覆盖所有不带声调的拼音，已带声调的拼音不会被修改。建议确认已备份数据库。是否继续？",
  );
  if (!confirmed) return;

  const button = els.recomputeTonelessPinyinButton;
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = "重算中...";
  showMaintenanceResult("正在扫描整库并重算无声调拼音，请稍候。");

  try {
    const payload = await postJSON("/api/maintenance/recompute-toneless-pinyin", {});
    const result = payload.result;
    const summary =
      `已扫描 ${result.scanned} 条，发现 ${result.matched} 条无声调拼音，实际更新 ${result.updated} 条。`;
    showMaintenanceResult(summary);
    await loadManageEntries();
    showToast(summary);
  } catch (error) {
    showMaintenanceResult(error.message);
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = previousText;
  }
}

async function capRejectedWeights() {
  if (!els.capRejectedWeightsButton) return;
  const confirmed = window.confirm(
    "将把所有人工拒绝词条的词频截断为 min(10, 原词频)，建议确认已备份数据库。是否继续？",
  );
  if (!confirmed) return;

  const button = els.capRejectedWeightsButton;
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = "处理中...";
  showMaintenanceResult("正在截断人工拒绝词条的词频，请稍候。");

  try {
    const payload = await postJSON("/api/maintenance/cap-rejected-weights", {});
    const result = payload.result;
    const summary = `已将 ${result.updated} 条人工拒绝词条的词频截断为不超过 ${result.cap}。`;
    showMaintenanceResult(summary);
    await refreshStats(payload.stats);
    await loadManageEntries();
    showToast(summary);
  } catch (error) {
    showMaintenanceResult(error.message);
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = previousText;
  }
}

async function reprocessOutdatedAi() {
  if (!els.reprocessOutdatedAiButton) return;
  const outdated = Number(state.ai.overview?.queue?.outdated || 0);
  if (outdated <= 0) {
    showMaintenanceResult("当前没有旧 prompt 版本的 AI 标注需要重算。");
    showToast("当前没有旧 prompt 版本的 AI 标注需要重算。");
    return;
  }

  const confirmed = window.confirm(
    `将允许后台 AI 重新处理 ${outdated} 条旧 prompt 版本的待定词条。是否继续？`,
  );
  if (!confirmed) return;

  const button = els.reprocessOutdatedAiButton;
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = "已加入...";
  showMaintenanceResult("已允许后台 AI 处理旧 prompt 版本词条，请保持自动 AI 标注开启。");

  try {
    const payload = await postJSON("/api/maintenance/reprocess-outdated-ai", {});
    applyAiOverview(payload.overview);
    const count = Number(payload.overview?.queue?.outdated || outdated);
    const summary = `已加入 AI 过时重算队列：${count} 条。`;
    showMaintenanceResult(summary);
    showToast(summary);
  } catch (error) {
    showMaintenanceResult(error.message);
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = previousText;
  }
}

function showMaintenanceResult(message) {
  if (!els.maintenanceResult) return;
  els.maintenanceResult.hidden = false;
  els.maintenanceResult.textContent = message;
}

async function ensureReviewEntry(forceAdvanceIfEmpty = false) {
  if (!forceAdvanceIfEmpty && getCurrentReviewEntry()) {
    state.review.canGoBack = state.review.pointer > 0;
    paintReviewEntry(getCurrentReviewEntry());
    return;
  }

  try {
    const payload = await fetchJSON("/api/review/state");
    applyServerCurrentPayload(payload, { resetLocalHistory: true, animate: false });
    if (forceAdvanceIfEmpty && !payload.current_entry) {
      await advanceReview("next", false);
    }
  } catch (error) {
    renderReviewEmpty(error.message, false);
  }
}

function getCurrentReviewEntry() {
  if (state.review.pointer < 0) return null;
  return state.review.timeline[state.review.pointer] || null;
}

async function advanceReview(direction = "next", animate = true) {
  if (state.review.loading) return;
  state.review.loading = true;
  try {
    const payload = await postJSON("/api/review/next", {
      prefer_ai: state.review.preferAi,
    });
    applyServerCurrentPayload(payload, {
      direction,
      appendIfNew: true,
      animate,
    });
  } catch (error) {
    renderReviewEmpty(error.message, state.review.canGoBack);
  } finally {
    state.review.loading = false;
  }
}

function jumpToReviewHistory(target) {
  if (!state.review.timeline.length) return;

  const latestIndex = state.review.timeline.length - 1;
  let nextPointer = latestIndex;
  let direction = "next";

  if (target && target !== "latest") {
    const [, rawIndex] = String(target).split(":");
    const parsedIndex = Number(rawIndex);
    if (!Number.isInteger(parsedIndex) || parsedIndex < 0 || parsedIndex > latestIndex) {
      updateReviewHistorySelect();
      return;
    }
    nextPointer = parsedIndex;
    direction = parsedIndex < state.review.pointer ? "back" : "next";
  }

  state.review.pointer = nextPointer;
  state.review.canGoBack = state.review.pointer > 0;
  updateReviewHistorySelect();
  if (els.reviewHistoryMenu) {
    els.reviewHistoryMenu.removeAttribute("open");
  }
  renderReviewEntry(getCurrentReviewEntry(), direction);
}

function goBackReviewHistory() {
  if (!state.review.timeline.length || state.review.pointer <= 0) {
    showToast("已经是本次历史的第一条。", true);
    return;
  }

  state.review.pointer -= 1;
  state.review.canGoBack = state.review.pointer > 0;
  updateReviewHistorySelect();
  renderReviewEntry(getCurrentReviewEntry(), "back");
}

function updateReviewHistorySelect() {
  if (!els.reviewHistoryDropdown) return;

  if (!state.review.timeline.length) {
    els.reviewHistoryDropdown.innerHTML =
      '<button class="history-option active" type="button" data-history-target="latest">最新待定词</button>';
    if (els.reviewHistoryMenu) {
      els.reviewHistoryMenu.classList.add("is-empty");
    }
    return;
  }

  const latestIndex = state.review.timeline.length - 1;
  const latestEntry = state.review.timeline[latestIndex];
  const currentTarget =
    state.review.pointer < 0 || state.review.pointer >= latestIndex ? "latest" : `history:${state.review.pointer}`;
  const options = [
    renderHistoryOption(
      "latest",
      `最新待定词 · ${latestEntry.phrase}`,
      latestEntry.status,
      currentTarget === "latest"
    ),
  ];

  for (let index = latestIndex - 1, offset = 1; index >= 0; index -= 1, offset += 1) {
    const entry = state.review.timeline[index];
    options.push(
      renderHistoryOption(
        `history:${index}`,
        `${offset} 条前 · ${entry.phrase}`,
        entry.status,
        currentTarget === `history:${index}`
      )
    );
  }

  els.reviewHistoryDropdown.innerHTML = options.join("");
  if (els.reviewHistoryMenu) {
    els.reviewHistoryMenu.classList.remove("is-empty");
  }
}

function renderHistoryOption(target, label, status, active = false) {
  return `
    <button class="history-option ${active ? "active" : ""}" type="button" data-history-target="${escapeHtml(target)}">
      <span class="history-option-label">${escapeHtml(label)}</span>
      <span class="history-option-status ${escapeHtml(status)}">${escapeHtml(STATUS_LABELS[status] || "待定")}</span>
    </button>
  `;
}

function applyServerCurrentPayload(
  payload,
  { direction = "next", resetLocalHistory = false, appendIfNew = false, animate = true } = {}
) {
  state.review.mode = "random";

  if (payload.stats) {
    void refreshStats(payload.stats);
  }

  if (resetLocalHistory) {
    state.review.timeline = payload.current_entry ? [payload.current_entry] : [];
    state.review.pointer = payload.current_entry ? 0 : -1;
    state.review.canGoBack = false;
  } else if (payload.current_entry) {
    const current = getCurrentReviewEntry();
    if (!current) {
      state.review.timeline = [payload.current_entry];
      state.review.pointer = 0;
    } else if (appendIfNew && current.id !== payload.current_entry.id) {
      if (state.review.pointer < state.review.timeline.length - 1) {
        state.review.timeline = state.review.timeline.slice(0, state.review.pointer + 1);
      }
      state.review.timeline.push(payload.current_entry);
      state.review.pointer = state.review.timeline.length - 1;
    } else {
      state.review.timeline[state.review.pointer] = payload.current_entry;
    }
    state.review.canGoBack = state.review.pointer > 0;
  } else if (resetLocalHistory) {
    state.review.timeline = [];
    state.review.pointer = -1;
    state.review.canGoBack = false;
  }

  updateReviewHistorySelect();

  if (payload.current_entry) {
    if (animate) {
      renderReviewEntry(payload.current_entry, direction);
    } else {
      paintReviewEntry(payload.current_entry);
    }
  } else {
    renderReviewEmpty("", state.review.canGoBack, animate);
  }
}

async function labelCurrent(status) {
  const current = getCurrentReviewEntry();
  if (!current) {
    showToast("当前没有待标注词条。", true);
    return;
  }

  if (status === "pending") {
    await skipCurrentReview();
    return;
  }

  flashReviewCard(status);
  const isReviewingHistory = state.review.pointer < state.review.timeline.length - 1;

  try {
    if (isReviewingHistory) {
      const payload = await postJSON(`/api/entries/${current.id}/status`, { status });
      syncEntryAcrossViews(payload.entry, { renderReview: false });
      if (status !== "pending") {
        state.review.timeline[state.review.pointer] = payload.entry;
      }
      incrementReviewMarkedCount();
      state.review.pointer += 1;
      state.review.canGoBack = state.review.pointer > 0;
      updateReviewHistorySelect();
      paintReviewEntry(getCurrentReviewEntry());
      void refreshStats(payload.stats);
      void loadManageEntries();
      return;
    }

    const payload = await postJSON("/api/review/label", {
      entry_id: current.id,
      status,
      prefer_ai: state.review.preferAi,
    });
    syncEntryAcrossViews(payload.updated_entry, { renderReview: false });
    incrementReviewMarkedCount();
    applyLabelResponse(payload);
    void loadManageEntries();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function skipCurrentReview() {
  const current = getCurrentReviewEntry();
  if (!current) {
    showToast("当前没有可跳过词条。", true);
    return;
  }

  flashReviewCard("pending");
  if (state.review.pointer < state.review.timeline.length - 1) {
    state.review.pointer += 1;
    state.review.canGoBack = state.review.pointer > 0;
    updateReviewHistorySelect();
    renderReviewEntry(getCurrentReviewEntry(), "next");
    return;
  }

  await advanceReview("next", true);
}

function incrementReviewMarkedCount() {
  state.review.markedCount += 1;
  updateReviewMarkedCount();
}

function updateReviewMarkedCount() {
  if (!els.reviewSessionCount) return;
  els.reviewSessionCount.textContent = `本次已标注 ${state.review.markedCount}`;
}

async function agreeWithAiSuggestion() {
  const current = getCurrentReviewEntry();
  if (!canAgreeWithAiSuggestion(current)) return;
  await labelCurrent(current.ai_label);
}

function canAgreeWithAiSuggestion(entry = getCurrentReviewEntry()) {
  const actionableLabels = ["accepted", "rejected"];
  return (
    actionableLabels.includes(entry?.ai_label) &&
    entry.status !== entry.ai_label
  );
}

function renderReviewEntry(entry, direction = "next") {
  animateReviewCard(direction);
  paintReviewEntry(entry);
}

function bindReviewWordAutoFit() {
  window.addEventListener("resize", scheduleReviewWordFit);
  if (document.fonts?.ready) {
    document.fonts.ready.then(scheduleReviewWordFit).catch(() => {});
  }
}

function scheduleReviewWordFit() {
  if (!els.reviewWord) return;
  window.cancelAnimationFrame(scheduleReviewWordFit.frameId || 0);
  scheduleReviewWordFit.frameId = window.requestAnimationFrame(fitReviewWordToOneLine);
}

function fitReviewWordToOneLine() {
  const word = els.reviewWord;
  const card = els.reviewCard;
  if (!word || !card) return;

  word.style.fontSize = "";
  word.style.transform = "";

  const cardStyle = window.getComputedStyle(card);
  const horizontalPadding =
    (Number.parseFloat(cardStyle.paddingLeft) || 0) +
    (Number.parseFloat(cardStyle.paddingRight) || 0);
  const availableWidth = Math.max(1, card.clientWidth - horizontalPadding);
  if (word.scrollWidth <= availableWidth) return;

  const computed = window.getComputedStyle(word);
  const maxFontSize = Number.parseFloat(computed.fontSize) || 96;
  let low = 20;
  let high = maxFontSize;
  for (let index = 0; index < 14; index += 1) {
    const mid = (low + high) / 2;
    word.style.fontSize = `${mid}px`;
    if (word.scrollWidth > availableWidth) {
      high = mid;
    } else {
      low = mid;
    }
  }

  word.style.fontSize = `${low}px`;
  if (word.scrollWidth > availableWidth) {
    word.style.transform = `scaleX(${availableWidth / word.scrollWidth})`;
  }
}

function paintReviewEntry(entry) {
  if (!els.reviewCard) return;
  els.reviewStatus.textContent = STATUS_LABELS[entry.status];
  els.reviewStatus.className = `status-pill ${entry.status}`;
  paintReviewAiStatus(entry);
  els.reviewWord.textContent = entry.phrase;
  els.reviewPinyin.textContent = entry.pinyin || "暂无拼音";
  els.reviewPinyinInput.value = entry.pinyin || "";
  els.reviewPinyinInput.disabled = false;
  els.reviewAutoPinyinButton.disabled = false;
  els.reviewSavePinyinButton.disabled = false;
  paintReviewWeight(entry);
  els.reviewImportedAt.textContent = `导入 ${formatDate(entry.imported_at)}`;
  els.reviewLabeledAt.textContent = `标注 ${entry.labeled_at ? formatDate(entry.labeled_at) : "未标注"}`;
  els.reviewAiScore.textContent =
    entry.ai_score == null ? "AI分数 -" : `AI分数 ${Number(entry.ai_score).toFixed(2)}`;
  updateReviewAiSuggestion(entry);
  updateReviewHistorySelect();
  scheduleReviewWordFit();
}

function renderReviewEmpty(message = "", canGoBack = false, animate = true) {
  if (!els.reviewCard) return;
  if (animate) {
    animateReviewCard("next");
  }
  els.reviewStatus.textContent = "待定";
  els.reviewStatus.className = "status-pill pending";
  if (els.reviewAiStatus) {
    els.reviewAiStatus.textContent = AI_STATUS_LABELS.unknown;
    els.reviewAiStatus.className = "status-pill ai unknown";
  }
  els.reviewWord.textContent = "当前没有待定词条";
  els.reviewPinyin.textContent = message || "可以去导入更多词库，或回到上一个已标注词条继续调整。";
  els.reviewPinyinInput.value = "";
  els.reviewPinyinInput.disabled = true;
  els.reviewAutoPinyinButton.disabled = true;
  els.reviewSavePinyinButton.disabled = true;
  els.reviewWeight.textContent = "词频 -";
  els.reviewWeight.className = "review-weight-badge";
  els.reviewImportedAt.textContent = "导入时间 -";
  els.reviewLabeledAt.textContent = "标注时间 -";
  if (els.reviewAiScore) {
    els.reviewAiScore.textContent = "AI分数 -";
  }
  if (els.reviewAgreeAiButton) {
    els.reviewAgreeAiButton.hidden = true;
    els.reviewAgreeAiButton.disabled = true;
  }
  updateReviewHistorySelect();
  scheduleReviewWordFit();
}

function paintReviewAiStatus(entry) {
  if (!els.reviewAiStatus) return;
  const aiLabel = entry.ai_label || "unknown";
  els.reviewAiStatus.textContent = AI_STATUS_LABELS[aiLabel] || AI_STATUS_LABELS.unknown;
  els.reviewAiStatus.className = `status-pill ai ${aiLabel}`;
}

function updateReviewAiSuggestion(entry) {
  if (!els.reviewAgreeAiButton) return;
  const aiLabel = entry.ai_label;
  if (!aiLabel || aiLabel === "pending") {
    els.reviewAgreeAiButton.hidden = true;
    els.reviewAgreeAiButton.disabled = true;
    els.reviewAgreeAiButton.textContent = "同意AI建议";
    els.reviewAgreeAiButton.classList.remove("ai-accept", "ai-reject", "ai-pending");
    return;
  }

  els.reviewAgreeAiButton.hidden = false;
  els.reviewAgreeAiButton.classList.remove("ai-accept", "ai-reject", "ai-pending");
  if (aiLabel === "accepted") {
    els.reviewAgreeAiButton.classList.add("ai-accept");
  } else if (aiLabel === "rejected") {
    els.reviewAgreeAiButton.classList.add("ai-reject");
  } else {
    els.reviewAgreeAiButton.classList.add("ai-pending");
  }
  const actionLabel = STATUS_LABELS[aiLabel] || "接受";
  const scoreText = entry.ai_score == null ? "" : ` · ${Number(entry.ai_score).toFixed(2)}`;
  if (entry.status === aiLabel) {
    els.reviewAgreeAiButton.disabled = true;
    els.reviewAgreeAiButton.textContent = `已与AI建议一致 - ${actionLabel}${scoreText}`;
  } else {
    els.reviewAgreeAiButton.disabled = false;
    els.reviewAgreeAiButton.textContent = `同意AI建议 - ${actionLabel}${scoreText}`;
  }
}

function animateReviewCard(direction) {
  if (!els.reviewCard) return;
  els.reviewCard.classList.remove("slide-next", "slide-back");
  void els.reviewCard.offsetWidth;
  els.reviewCard.classList.add(direction === "back" ? "slide-back" : "slide-next");
}

function flashReviewCard(status) {
  if (!els.reviewCard) return;
  els.reviewCard.classList.remove("flash-accepted", "flash-pending", "flash-rejected");
  els.reviewCard.classList.add(`flash-${status}`);
  window.setTimeout(() => {
    els.reviewCard.classList.remove(`flash-${status}`);
  }, 320);
}

async function loadManageEntries() {
  if (!els.entryList) return;
  try {
    const params = new URLSearchParams({
      page: String(state.manage.page),
      page_size: String(state.manage.pageSize),
      status: state.manage.status,
      ai_status: state.manage.aiStatus,
      q: state.manage.query,
    });
    if (state.manage.minWeight) {
      params.set("min_weight", state.manage.minWeight);
    }
    if (state.manage.maxWeight) {
      params.set("max_weight", state.manage.maxWeight);
    }

    const payload = await fetchJSON(`/api/entries?${params.toString()}`);
    state.manage.currentItems = payload.items;
    state.manage.totalPages = payload.total_pages;
    state.manage.page = payload.page;
    renderEntryList(payload.items);
    updateSelectedCount();
    els.pageInfo.textContent = `第 ${payload.page} / ${payload.total_pages} 页，共 ${payload.total} 条`;
    els.pagePrev.disabled = payload.page <= 1;
    els.pageNext.disabled = payload.page >= payload.total_pages;
    if (els.pageJumpInput) {
      els.pageJumpInput.max = String(payload.total_pages);
      els.pageJumpInput.value = String(payload.page);
    }
  } catch (error) {
    els.entryList.innerHTML = `
      <div class="table-empty">
        <strong>加载失败</strong>
        <span>${escapeHtml(error.message)}</span>
      </div>
    `;
  }
}

async function jumpToManagePage() {
  if (!els.pageJumpInput) return;
  const nextPage = Number(els.pageJumpInput.value);
  if (!Number.isInteger(nextPage)) {
    showToast("请输入合法页码。", true);
    return;
  }

  state.manage.page = Math.min(Math.max(nextPage, 1), state.manage.totalPages);
  await loadManageEntries();
}

function renderEntryList(items) {
  if (!items.length) {
    els.entryList.innerHTML =
      '<div class="table-empty"><strong>没有匹配词条</strong><span>调整搜索条件后再试试。</span></div>';
    return;
  }

  els.entryList.innerHTML = `
    <table class="entry-table">
      <colgroup>
        <col class="col-select" />
        <col class="col-id" />
        <col class="col-phrase" />
        <col class="col-pinyin" />
        <col class="col-weight" />
        <col class="col-status" />
        <col class="col-ai-status" />
        <col class="col-time" />
        <col class="col-time" />
        <col class="col-actions" />
      </colgroup>
      <thead>
        <tr>
          <th class="col-select">选择</th>
          <th class="col-id">ID</th>
          <th class="col-phrase">词条</th>
          <th class="col-pinyin">拼音</th>
          <th class="col-weight">词频</th>
          <th class="col-status">状态</th>
          <th class="col-ai-status">AI 标注</th>
          <th class="col-time">导入时间</th>
          <th class="col-time">标注时间</th>
          <th class="col-actions">操作</th>
        </tr>
      </thead>
      <tbody>
        ${items.map(renderEntryRow).join("")}
      </tbody>
    </table>
  `;
}

function renderEntryRow(entry) {
  const selected = state.manage.selectedIds.has(entry.id);
  return `
    <tr class="${selected ? "is-selected" : ""}">
      <td class="col-select">
        <label class="entry-select">
          <input type="checkbox" data-select-entry-id="${entry.id}" ${selected ? "checked" : ""} />
        </label>
      </td>
      <td class="col-id">${entry.id}</td>
      <td class="col-phrase">
        <div class="table-phrase">${escapeHtml(entry.phrase)}</div>
      </td>
      <td class="col-pinyin">
        <div class="table-pinyin-cell">
          ${renderPinyinLockButton(entry)}
          <div class="table-pinyin">${escapeHtml(entry.pinyin)}</div>
        </div>
      </td>
      <td class="col-weight">${formatManageWeight(entry)}</td>
      <td class="col-status">
        <span class="status-pill ${entry.status} compact">${STATUS_LABELS[entry.status]}</span>
      </td>
      <td class="col-ai-status">
        ${renderAiStatusCell(entry)}
      </td>
      <td class="col-time">${escapeHtml(formatDate(entry.imported_at))}</td>
      <td class="col-time">${escapeHtml(entry.labeled_at ? formatDate(entry.labeled_at) : "未标注")}</td>
      <td class="col-actions">
        <div class="table-actions">
          <button class="ghost-button small-inline" data-edit-entry-id="${entry.id}">编辑</button>
          ${renderManageButton(entry, "accepted")}
          ${renderManageButton(entry, "pending")}
          ${renderManageButton(entry, "rejected")}
        </div>
      </td>
    </tr>
  `;
}

function renderPinyinLockButton(entry) {
  const locked = !!entry.pinyin_locked;
  const icon = locked ? "🔒" : "🔓";
  const label = locked ? "拼音已锁定，点击解锁" : "拼音未锁定，点击锁定";
  return `
    <button
      class="pinyin-lock-button has-tip ${locked ? "is-locked" : ""}"
      type="button"
      data-toggle-pinyin-lock-id="${entry.id}"
      aria-label="${label}"
      data-tip="${label}"
    >${icon}</button>
  `;
}

function renderAiStatusCell(entry) {
  const aiLabel = entry.ai_label || "unknown";
  const scoreText = entry.ai_score == null ? "" : ` · ${Number(entry.ai_score).toFixed(2)}`;
  return `
    <span class="status-pill ai ${escapeHtml(aiLabel)} compact">
      ${escapeHtml(AI_STATUS_LABELS[aiLabel] || AI_STATUS_LABELS.unknown)}${escapeHtml(scoreText)}
    </span>
  `;
}

function renderManageButton(entry, status) {
  const activeClass = entry.status === status ? `active ${status}` : "";
  return `
    <button class="table-status-button table-${status} ${activeClass}" data-entry-id="${entry.id}" data-status="${status}">
      ${STATUS_LABELS[status]}
    </button>
  `;
}

function updateSelectedCount() {
  if (!els.selectedCount || !els.bulkTargetSummary) return;
  const count = state.manage.selectedIds.size;
  els.selectedCount.textContent = `已选择 ${count} 条`;
  els.bulkTargetSummary.textContent = `将作用于 ${count} 条词条`;
}

function applyLabelResponse(payload) {
  state.review.mode = "random";
  if (payload.stats) {
    void refreshStats(payload.stats);
  }

  if (!payload.current_entry) {
    renderReviewEmpty("", state.review.pointer > 0);
    return;
  }

  const current = getCurrentReviewEntry();
  if (!current) {
    state.review.timeline = [payload.current_entry];
    state.review.pointer = 0;
  } else if (current.id !== payload.current_entry.id) {
    state.review.timeline.push(payload.current_entry);
    state.review.pointer = state.review.timeline.length - 1;
  } else {
    state.review.timeline[state.review.pointer] = payload.current_entry;
  }
  state.review.canGoBack = state.review.pointer > 0;
  updateReviewHistorySelect();
  renderReviewEntry(payload.current_entry, "next");
}

function getSelectedIds() {
  return [...state.manage.selectedIds];
}

async function applyBulkStatus(status) {
  const ids = getSelectedIds();
  if (!ids.length) {
    showToast("请先选择要处理的词条。", true);
    return;
  }

  try {
    const payload = await postJSON("/api/entries/bulk-update", {
      ids,
      updates: { status },
    });
    applyBulkResponse(payload, `已批量标记为${STATUS_LABELS[status]}。`);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function applyBulkPinyinLock(locked) {
  const ids = getSelectedIds();
  if (!ids.length) {
    showToast("请先选择要处理的词条。", true);
    return;
  }

  try {
    const payload = await postJSON("/api/entries/bulk-update", {
      ids,
      updates: { pinyin_locked: locked },
    });
    applyBulkResponse(payload, locked ? "已锁定所选词条拼音。" : "已解锁所选词条拼音。");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function toggleEntryPinyinLock(entryId) {
  const entry = state.manage.currentItems.find((item) => item.id === entryId);
  if (!entry) return;

  const nextLocked = !entry.pinyin_locked;
  try {
    const payload = await postJSON(`/api/entries/${entryId}/update`, {
      pinyin_locked: nextLocked,
    });
    syncEntryAcrossViews(payload.entry);
    await loadManageEntries();
    showToast(nextLocked ? "拼音已锁定。" : "拼音已解锁。");
  } catch (error) {
    showToast(error.message, true);
  }
}

function openBulkEditDialog() {
  if (!state.manage.selectedIds.size) {
    showToast("请先选择至少一条词条。", true);
    return;
  }

  els.bulkEditForm.reset();
  updateBulkFieldStates();
  updateSelectedCount();
  openDialog(els.bulkEditDialog);
}

function closeBulkEditDialog() {
  closeDialog(els.bulkEditDialog);
}

function updateBulkFieldStates() {
  els.bulkStatus.disabled = !els.bulkApplyStatus.checked;
  els.bulkWeight.disabled = !els.bulkApplyWeight.checked;
  els.bulkImportedAt.disabled = !els.bulkApplyImportedAt.checked;
  els.bulkPinyin.disabled = !els.bulkApplyPinyin.checked || els.bulkRegeneratePinyin.checked;
  els.bulkLabeledAt.disabled = !els.bulkApplyLabeledAt.checked || els.bulkClearLabeledAt.checked;
}

async function saveBulkEditForm() {
  const ids = getSelectedIds();
  if (!ids.length) {
    showToast("请先选择至少一条词条。", true);
    return;
  }

  const updates = {};
  if (els.bulkApplyStatus.checked) {
    updates.status = els.bulkStatus.value;
  }
  if (els.bulkApplyWeight.checked) {
    updates.weight = els.bulkWeight.value.trim();
  }
  if (els.bulkApplyImportedAt.checked) {
    updates.imported_at = fromDatetimeLocalValue(els.bulkImportedAt.value);
  }
  if (els.bulkApplyPinyin.checked) {
    updates.pinyin = els.bulkPinyin.value.trim();
  }
  if (els.bulkApplyLabeledAt.checked) {
    updates.labeled_at = fromDatetimeLocalValue(els.bulkLabeledAt.value);
  }

  try {
    const payload = await postJSON("/api/entries/bulk-update", {
      ids,
      updates,
      regenerate_pinyin: els.bulkRegeneratePinyin.checked,
      clear_labeled_at: els.bulkClearLabeledAt.checked,
    });
    closeBulkEditDialog();
    applyBulkResponse(payload, `已批量更新 ${payload.updated_count} 条词条。`);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function applyBulkResponse(payload, message) {
  payload.entries.forEach(syncEntryAcrossViews);
  await refreshStats(payload.stats);
  void loadAiOverview(false);
  await loadManageEntries();
  showToast(message);
}

async function saveCurrentReviewPinyin(useGeneratedPinyin) {
  const current = getCurrentReviewEntry();
  if (!current) {
    showToast("当前没有可编辑的词条。", true);
    return;
  }

  try {
    let pinyin = els.reviewPinyinInput.value.trim();
    if (useGeneratedPinyin || !pinyin) {
      pinyin = await fetchGeneratedPinyin(current.phrase);
      els.reviewPinyinInput.value = pinyin;
    }

    const payload = await postJSON(`/api/entries/${current.id}/update`, { pinyin });
    syncEntryAcrossViews(payload.entry);
    await refreshStats(payload.stats);
    await loadManageEntries();
    showToast("拼音已保存。");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function fillReviewPinyinFromPhrase() {
  const current = getCurrentReviewEntry();
  if (!current) {
    showToast("当前没有可编辑的词条。", true);
    return;
  }

  try {
    const pinyin = await fetchGeneratedPinyin(current.phrase);
    els.reviewPinyinInput.value = pinyin;
    showToast("已按当前词条自动生成拼音。");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function openEditDialog(entryId) {
  try {
    const entry = await fetchJSON(`/api/entries/${entryId}`);
    state.edit.entryId = entry.id;
    state.edit.forceClearLabeledAt = false;
    state.edit.originalEntry = entry;
    els.editEntryId.value = String(entry.id);
    els.editPhrase.value = entry.phrase;
    els.editPinyin.value = entry.pinyin || "";
    els.editWeight.value = String(entry.weight);
    els.editStatus.value = entry.status;
    els.editImportedAt.value = toDatetimeLocalValue(entry.imported_at);
    els.editLabeledAt.value = toDatetimeLocalValue(entry.labeled_at);
    openDialog(els.entryEditDialog);
  } catch (error) {
    showToast(error.message, true);
  }
}

function closeEditDialog() {
  state.edit.entryId = null;
  state.edit.forceClearLabeledAt = false;
  state.edit.originalEntry = null;
  closeDialog(els.entryEditDialog);
}

async function fillEditPinyinFromPhrase() {
  const phrase = els.editPhrase.value.trim();
  if (!phrase) {
    showToast("请先填写词条。", true);
    return;
  }

  try {
    els.editPinyin.value = await fetchGeneratedPinyin(phrase);
    showToast("已根据词条自动生成拼音。");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function saveEditForm() {
  const entryId = Number(els.editEntryId.value);
  if (!entryId) {
    showToast("缺少词条 ID。", true);
    return;
  }

  const payload = {
    phrase: els.editPhrase.value.trim(),
    pinyin: els.editPinyin.value.trim(),
    status: els.editStatus.value,
    imported_at: fromDatetimeLocalValue(els.editImportedAt.value),
  };

  const nextWeight = els.editWeight.value.trim();
  if (nextWeight !== String(state.edit.originalEntry?.weight ?? "")) {
    payload.weight = nextWeight;
  }

  if (els.editLabeledAt.value) {
    payload.labeled_at = fromDatetimeLocalValue(els.editLabeledAt.value);
  } else if (state.edit.forceClearLabeledAt) {
    payload.labeled_at = "";
  }

  const originalEntry = state.edit.originalEntry;
  const unlocksPinyin =
    !!originalEntry?.pinyin_locked && payload.phrase !== String(originalEntry.phrase || "").trim();

  try {
    const response = await postJSON(`/api/entries/${entryId}/update`, payload);
    syncEntryAcrossViews(response.entry);
    await refreshStats(response.stats);
    void loadAiOverview(false);
    await loadManageEntries();
    closeEditDialog();
    showToast(
      unlocksPinyin
        ? "词条信息已更新；因词条内容变化，拼音锁定已自动解除。"
        : "词条信息已更新。",
    );
  } catch (error) {
    showToast(error.message, true);
  }
}

function syncEntryAcrossViews(entry, options = {}) {
  const { renderReview = true, animate = false } = options;
  state.review.timeline = state.review.timeline.map((item) => (item.id === entry.id ? entry : item));
  state.manage.currentItems = state.manage.currentItems.map((item) => (item.id === entry.id ? entry : item));
  updateReviewHistorySelect();

  const current = getCurrentReviewEntry();
  if (renderReview && current && current.id === entry.id) {
    if (animate) {
      renderReviewEntry(entry);
    } else {
      paintReviewEntry(entry);
    }
  }
}

function getSelectedExportStatuses() {
  return [...document.querySelectorAll('input[name="exportStatus"]:checked')].map((input) => input.value);
}

function syncMixedExportOptions() {
  if (!els.includeMixedWords || !els.mixedPinyinScheme) return;
  els.mixedPinyinScheme.disabled = !els.includeMixedWords.checked;
}

async function updateExportCount() {
  if (!els.exportCountNote) return;

  const statuses = getSelectedExportStatuses();
  if (!statuses.length) {
    els.exportCountNote.textContent = "请至少选择一个导出状态。";
    return;
  }

  try {
    const params = new URLSearchParams({ statuses: statuses.join(",") });
    params.set("include_ai_assist", els.includeAiAssist?.checked ? "1" : "0");
    params.set("include_mixed", els.includeMixedWords?.checked ? "1" : "0");
    const payload = await fetchJSON(`/api/export/count?${params.toString()}`);
    const exportMode = els.includeMixedWords?.checked
      ? "仅导出中英混杂/全英文专用词典"
      : "导出普通词典，不含中英混杂/全英文词条";
    const extras = [
      els.includeAiAssist?.checked ? "含 AI 辅助" : "",
    ].filter(Boolean);
    els.exportCountNote.textContent = `当前选择将导出 ${payload.count} 条词条（${[exportMode, ...extras].join("，")}）。`;
  } catch (error) {
    els.exportCountNote.textContent = `无法计算导出数量：${error.message}`;
  }
}

function setImportBusy(isBusy, detail = "") {
  if (!els.importLoadingOverlay) return;

  els.importLoadingOverlay.hidden = !isBusy;
  if (els.importLoadingText) {
    els.importLoadingText.textContent = isBusy
      ? `${detail}，请稍候。导入完成前请不要重复点击或切换操作。`
      : "正在处理词库，请稍候。导入完成前请不要重复点击或切换操作。";
  }

  if (els.importForm) {
    els.importForm.querySelectorAll("input, textarea, button").forEach((element) => {
      element.disabled = isBusy;
    });
  }
  if (els.exportForm) {
    els.exportForm.querySelectorAll("input, button").forEach((element) => {
      element.disabled = isBusy;
    });
  }
}


async function fetchGeneratedPinyin(phrase) {
  const params = new URLSearchParams({ phrase });
  const payload = await fetchJSON(`/api/pinyin?${params.toString()}`);
  return payload.pinyin;
}

function openDialog(dialog) {
  if (!dialog) return;
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
  } else {
    dialog.setAttribute("open", "open");
  }
}

function closeDialog(dialog) {
  if (!dialog) return;
  if (typeof dialog.close === "function") {
    dialog.close();
  } else {
    dialog.removeAttribute("open");
  }
}

function showMessage(message, isError = false) {
  if (!els.importMessage) {
    showToast(message, isError);
    return;
  }
  els.importMessage.textContent = message;
  els.importMessage.classList.add("show");
  els.importMessage.style.background = isError ? "rgba(188, 63, 50, 0.12)" : "rgba(13, 103, 114, 0.09)";
  els.importMessage.style.color = isError ? "#bc3f32" : "#0d6772";
}

function showToast(message, isError = false) {
  if (!els.toast) return;
  els.toast.textContent = message;
  els.toast.style.background = isError ? "rgba(137, 27, 27, 0.92)" : "rgba(16, 47, 51, 0.92)";
  els.toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    els.toast.classList.remove("show");
  }, Math.max(2200, Math.min(5200, 1800 + message.length * 32)));
}

async function fetchJSON(url) {
  const response = await fetch(url, {
    headers: {
      "X-Review-Session": getReviewSessionKey(),
    },
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "请求失败。");
  }
  return payload;
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Review-Session": getReviewSessionKey(),
    },
    body: JSON.stringify(body),
  });

  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "请求失败。");
  }
  return payload;
}

async function postRawText(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "X-Review-Session": getReviewSessionKey(),
    },
    body,
  });

  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "请求失败。");
  }
  return payload;
}

function formatDate(rawValue) {
  if (!rawValue) return "-";
  const date = new Date(rawValue);
  if (Number.isNaN(date.getTime())) return rawValue;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatReviewWeight(entry) {
  if (!entry?.weight_defined) return "词频未定义";
  return `词频 ${entry.weight}`;
}

function paintReviewWeight(entry) {
  if (!els.reviewWeight) return;
  els.reviewWeight.textContent = formatReviewWeight(entry);
  els.reviewWeight.className = `review-weight-badge ${getReviewWeightTone(entry)}`;
}

function getReviewWeightTone(entry) {
  if (!entry?.weight_defined) return "weight-unknown";
  const weight = Number(entry?.weight ?? 1);
  if (Number.isFinite(weight) && weight > 10) return "weight-high";
  if (Number.isFinite(weight) && weight >= 2) return "weight-mid";
  return "weight-low";
}

function formatManageWeight(entry) {
  const weight = escapeHtml(String(entry?.weight ?? 1));
  if (entry?.weight_defined) return weight;
  return `${weight}<span class="default-weight-note">（默认）</span>`;
}

function toDatetimeLocalValue(rawValue) {
  if (!rawValue) return "";
  const date = new Date(rawValue);
  if (Number.isNaN(date.getTime())) {
    return String(rawValue).slice(0, 16);
  }

  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  return `${year}-${month}-${day}T${hour}:${minute}`;
}

function fromDatetimeLocalValue(rawValue) {
  if (!rawValue) return "";
  return `${rawValue}:00`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
