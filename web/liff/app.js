"use strict";

(() => {
  const yen = new Intl.NumberFormat("ja-JP");
  const STATUS_LABELS = { confirmed: "確定", hold: "保留", tentative: "未確定" };
  const LOGIN_RETRY_KEY = "ledger-login-retry";
  const EDITABLE_FIELDS = [
    "service_date",
    "provider_name",
    "family_member_name",
    "medical_category",
    "amount_yen",
    "reimbursed_yen",
    "note",
    "status",
  ];

  const state = {
    config: null,
    meta: null,
    year: null,
    entries: [],
    summary: null,
    issues: [],
    readyForExport: true,
    tab: "entries",
    memberFilter: "",
    issuesOnly: false,
    editing: null, // { entry, initial } 新規追加なら entry は null
  };

  const $ = (id) => document.getElementById(id);

  // ---------- DOM ヘルパー（利用者のデータは必ず textContent で入れる） ----------
  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "className") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function formatYen(value) {
    return `${yen.format(Number(value) || 0)}円`;
  }

  function formatDate(entry) {
    const date = entry.effective_date || entry.service_date;
    if (!date) return "日付なし";
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
    if (!match) return date;
    return `${Number(match[2])}/${Number(match[3])}`;
  }

  function setStatus(message, isError = false) {
    const node = $("status-message");
    node.textContent = message || "";
    node.hidden = !message;
    node.classList.toggle("error", Boolean(isError));
  }

  // ---------- API ----------
  class ApiError extends Error {
    constructor(status, body) {
      super((body && body.message) || `通信に失敗しました（${status}）`);
      this.status = status;
      this.body = body || {};
    }
  }

  async function api(method, path, body) {
    const headers = { "X-Line-Id-Token": liff.getIDToken() || "" };
    const options = { method, headers, cache: "no-store" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(`${state.config.apiBase}${path}`, options);
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    if (response.status === 401) {
      reLogin();
      throw new ApiError(401, payload);
    }
    if (!response.ok) throw new ApiError(response.status, payload);
    sessionStorage.removeItem(LOGIN_RETRY_KEY);
    return payload;
  }

  function reLogin() {
    // ID トークンの期限切れ。ログインし直して同じ画面に戻る（無限ループを避けるため1回だけ）
    if (sessionStorage.getItem(LOGIN_RETRY_KEY)) {
      setStatus("ログインできませんでした。LINEのトーク画面から開き直してください。", true);
      return;
    }
    sessionStorage.setItem(LOGIN_RETRY_KEY, "1");
    if (liff.isLoggedIn()) liff.logout();
    liff.login({ redirectUri: window.location.href });
  }

  // ---------- 読み込み ----------
  async function boot() {
    try {
      const response = await fetch("config.json", { cache: "no-store" });
      state.config = await response.json();
    } catch (_) {
      setStatus("設定を読み込めませんでした。", true);
      return;
    }
    state.config.apiBase = state.config.apiBase || "/api";
    if (!state.config.liffId) {
      setStatus("LIFF ID が設定されていません。管理者に連絡してください。", true);
      return;
    }
    try {
      await liff.init({ liffId: state.config.liffId });
    } catch (error) {
      setStatus("LINEとの接続に失敗しました。LINEのトーク画面から開き直してください。", true);
      return;
    }
    if (!liff.isLoggedIn()) {
      liff.login({ redirectUri: window.location.href });
      return;
    }

    try {
      state.meta = await api("GET", "/meta");
    } catch (error) {
      if (error.status !== 401) setStatus(error.message, true);
      return;
    }
    setupStaticControls();
    await loadYear(defaultYear());
  }

  function defaultYear() {
    const fromQuery = Number(new URLSearchParams(window.location.search).get("year"));
    if (fromQuery >= 2000 && fromQuery <= 2100) return fromQuery;
    // 1〜3月は確定申告の時期なので前年を開く
    const now = new Date();
    return now.getMonth() < 3 ? state.meta.current_year - 1 : state.meta.current_year;
  }

  async function loadYear(year) {
    state.year = year;
    renderYearOptions();
    $("app").setAttribute("aria-busy", "true");
    setStatus("読み込み中です…");
    try {
      const [list, checks] = await Promise.all([
        api("GET", `/entries?year=${year}`),
        api("GET", `/checks?year=${year}`),
      ]);
      state.entries = list.entries;
      state.summary = list.summary;
      state.issues = checks.issues;
      state.readyForExport = checks.ready_for_export;
      setStatus("");
      render();
    } catch (error) {
      if (error.status !== 401) setStatus(error.message, true);
    } finally {
      $("app").setAttribute("aria-busy", "false");
    }
  }

  // ---------- 描画 ----------
  function setupStaticControls() {
    $("year-select").addEventListener("change", (event) => loadYear(Number(event.target.value)));
    $("tab-entries").addEventListener("click", () => switchTab("entries"));
    $("tab-checks").addEventListener("click", () => switchTab("checks"));
    $("member-filter").addEventListener("change", (event) => {
      state.memberFilter = event.target.value;
      renderEntries();
    });
    $("issues-only").addEventListener("change", (event) => {
      state.issuesOnly = event.target.checked;
      renderEntries();
    });
    $("add-button").addEventListener("click", () => openDialog(null));
    $("cancel-button").addEventListener("click", () => $("entry-dialog").close());
    $("delete-button").addEventListener("click", deleteEditingEntry);
    $("entry-form").addEventListener("submit", (event) => {
      event.preventDefault();
      saveEditingEntry();
    });

    const categorySelect = $("entry-form").elements.medical_category;
    for (const category of state.meta.medical_categories) {
      categorySelect.append(el("option", { value: category, text: category }));
    }
    const datalist = $("family-options");
    for (const name of state.meta.family_members) {
      datalist.append(el("option", { value: name }));
    }
  }

  function renderYearOptions() {
    const years = new Set(state.meta.years);
    years.add(state.meta.current_year);
    years.add(state.meta.current_year - 1);
    years.add(state.year);
    const select = $("year-select");
    select.replaceChildren(
      ...[...years].sort((a, b) => b - a).map((year) => el("option", { value: year, text: `${year}年`, selected: year === state.year }))
    );
  }

  function render() {
    $("summary").hidden = false;
    $("tabs").hidden = false;
    $("add-button").hidden = false;
    renderSummary();
    renderMemberFilter();
    renderEntries();
    renderChecks();
    switchTab(state.tab);
  }

  function renderSummary() {
    const summary = state.summary;
    $("summary-total").textContent = formatYen(summary.total_amount_yen);
    $("summary-reimbursed").textContent = formatYen(summary.total_reimbursed_yen);
    $("summary-net").textContent = formatYen(summary.net_amount_yen);
    $("summary-count").textContent = `${summary.entry_count}件`;
    $("summary-unconfirmed").textContent = `${summary.unconfirmed_count}件`;
  }

  function renderMemberFilter() {
    const names = new Set(state.meta.family_members);
    for (const entry of state.entries) if (entry.family_member_name) names.add(entry.family_member_name);
    if (state.memberFilter && !names.has(state.memberFilter)) state.memberFilter = "";
    $("member-filter").replaceChildren(
      el("option", { value: "", text: "全員" }),
      ...[...names].map((name) => el("option", { value: name, text: name, selected: name === state.memberFilter }))
    );
  }

  function issuesByEntry() {
    const map = new Map();
    for (const issue of state.issues) {
      if (!map.has(issue.entry_id)) map.set(issue.entry_id, []);
      map.get(issue.entry_id).push(issue);
    }
    return map;
  }

  function renderEntries() {
    const byEntry = issuesByEntry();
    const visible = state.entries.filter((entry) => {
      if (state.memberFilter && entry.family_member_name !== state.memberFilter) return false;
      if (state.issuesOnly && !byEntry.has(entry.entry_id)) return false;
      return true;
    });
    $("entry-list").replaceChildren(...visible.map((entry) => renderEntry(entry, byEntry.get(entry.entry_id) || [])));
    $("entries-empty").hidden = visible.length > 0;
    $("entries-empty").textContent = state.entries.length ? "条件に合う明細はありません。" : "この年の明細はまだありません。";
  }

  function renderEntry(entry, issues) {
    const hasError = issues.some((issue) => issue.severity === "error");
    const hasWarning = issues.length > 0 && !hasError;
    const badges = [];
    if (entry.status !== "confirmed") badges.push(el("span", { className: "badge warning", text: STATUS_LABELS[entry.status] || entry.status }));
    if (entry.source === "manual") badges.push(el("span", { className: "badge", text: "手入力" }));
    if (issues.length) {
      badges.push(el("span", { className: `badge ${hasError ? "error" : "warning"}`, text: `要確認 ${issues.length}` }));
    }
    const button = el(
      "button",
      {
        type: "button",
        className: `entry${hasError ? " has-error" : ""}${hasWarning ? " has-warning" : ""}`,
        onclick: () => openDialog(entry),
      },
      [
        el("span", { className: "entry-provider", text: entry.provider_name || "（支払先なし）" }),
        el("span", { className: "entry-amount", text: entry.amount_yen == null ? "金額なし" : formatYen(entry.amount_yen) }),
        el("span", { className: "entry-meta" }, [
          el("span", { text: formatDate(entry) }),
          el("span", { text: entry.family_member_name || "（対象者なし）" }),
          el("span", { text: entry.medical_category }),
          entry.reimbursed_yen ? el("span", { text: `補填 ${formatYen(entry.reimbursed_yen)}` }) : null,
          ...badges,
        ]),
      ]
    );
    return el("li", {}, button);
  }

  function renderChecks() {
    const errors = state.issues.filter((issue) => issue.severity === "error").length;
    const warnings = state.issues.length - errors;
    const badge = $("checks-badge");
    badge.hidden = state.issues.length === 0;
    badge.textContent = String(state.issues.length);

    const lead = $("check-lead");
    if (!state.issues.length) {
      lead.textContent = "気になる点は見つかりませんでした。";
    } else if (errors) {
      lead.textContent = `直す必要がある項目が${errors}件、確認をおすすめする項目が${warnings}件あります。`;
    } else {
      lead.textContent = `確認をおすすめする項目が${warnings}件あります。問題なければそのままで構いません。`;
    }

    const entryById = new Map(state.entries.map((entry) => [entry.entry_id, entry]));
    $("issue-list").replaceChildren(
      ...state.issues.map((issue) => {
        const entry = entryById.get(issue.entry_id);
        const actions = [];
        if (entry) actions.push(el("button", { type: "button", text: "この明細を開く", onclick: () => openDialog(entry) }));
        for (const relatedId of issue.related_entry_ids || []) {
          const related = entryById.get(relatedId);
          if (related) {
            actions.push(el("button", { type: "button", text: `重複候補を開く（${formatDate(related)}）`, onclick: () => openDialog(related) }));
          }
        }
        return el("li", { className: `issue ${issue.severity}` }, [
          el("span", { className: `badge ${issue.severity}`, text: issue.severity === "error" ? "要修正" : "要確認" }),
          el("p", { className: "issue-message", text: issue.message }),
          entry
            ? el("div", {
                className: "issue-entry",
                text: `${formatDate(entry)}　${entry.provider_name || "（支払先なし）"}　${entry.amount_yen == null ? "" : formatYen(entry.amount_yen)}`,
              })
            : null,
          el("div", { className: "issue-actions" }, actions),
        ]);
      })
    );
  }

  function switchTab(tab) {
    state.tab = tab;
    const isEntries = tab === "entries";
    $("tab-entries").setAttribute("aria-selected", String(isEntries));
    $("tab-checks").setAttribute("aria-selected", String(!isEntries));
    $("panel-entries").hidden = !isEntries;
    $("panel-checks").hidden = isEntries;
  }

  // ---------- 編集ダイアログ ----------
  function entryToFormValues(entry) {
    if (!entry) {
      return {
        service_date: "",
        provider_name: "",
        family_member_name: state.memberFilter || "",
        medical_category: "その他の医療費",
        amount_yen: "",
        reimbursed_yen: "",
        note: "",
        status: "confirmed",
      };
    }
    return {
      // 年のない日付（月日だけ）は入力欄に入らないので空にし、入力し直してもらう
      service_date: /^\d{4}-\d{2}-\d{2}$/.test(entry.service_date || "") ? entry.service_date : "",
      provider_name: entry.provider_name || "",
      family_member_name: entry.family_member_name || "",
      medical_category: entry.medical_category,
      amount_yen: entry.amount_yen == null ? "" : String(entry.amount_yen),
      reimbursed_yen: entry.reimbursed_yen ? String(entry.reimbursed_yen) : "",
      note: entry.note || "",
      status: entry.status === "hold" ? "hold" : "confirmed",
    };
  }

  function openDialog(entry) {
    const form = $("entry-form");
    const initial = entryToFormValues(entry);
    state.editing = { entry, initial };
    for (const name of EDITABLE_FIELDS) form.elements[name].value = initial[name];
    clearErrors();

    $("dialog-title").textContent = entry ? "明細の編集" : "手入力で追加";
    let hint = "";
    if (!entry) hint = "交通費など、領収書のない医療費を追加できます。";
    else if (entry.source === "ocr") hint = "LINEで送った領収書から読み取った明細です。";
    if (entry && entry.status === "tentative") hint += " 内容を確認して保存すると「確定」になります。";
    $("dialog-hint").textContent = hint.trim();
    $("delete-button").hidden = !entry;
    $("save-button").disabled = false;
    $("entry-dialog").showModal();
  }

  function clearErrors() {
    $("form-error").textContent = "";
    for (const node of document.querySelectorAll("[data-error-for]")) node.textContent = "";
    for (const name of EDITABLE_FIELDS) $("entry-form").elements[name].removeAttribute("aria-invalid");
  }

  function showErrors(error) {
    const fields = (error.body && error.body.details && error.body.details.fields) || {};
    let shown = false;
    for (const [name, message] of Object.entries(fields)) {
      const target = document.querySelector(`[data-error-for="${CSS.escape(name)}"]`);
      if (!target) continue;
      target.textContent = message;
      $("entry-form").elements[name].setAttribute("aria-invalid", "true");
      shown = true;
    }
    $("form-error").textContent = shown ? "入力内容を確認してください。" : error.message;
  }

  function collectChanges() {
    const form = $("entry-form");
    const { entry, initial } = state.editing;
    const values = {};
    for (const name of EDITABLE_FIELDS) {
      const value = form.elements[name].value;
      if (!entry) {
        if (value !== "") values[name] = value;
      } else if (value !== initial[name]) {
        values[name] = value;
      }
    }
    // 未確定の明細を保存したら確定にする
    if (entry && entry.status === "tentative" && values.status === undefined) values.status = form.elements.status.value;
    return values;
  }

  async function saveEditingEntry() {
    const { entry } = state.editing;
    const changes = collectChanges();
    if (entry && Object.keys(changes).length === 0) {
      $("entry-dialog").close();
      return;
    }
    clearErrors();
    $("save-button").disabled = true;
    try {
      if (entry) await api("PATCH", `/entries/${encodeURIComponent(entry.entry_id)}`, changes);
      else await api("POST", "/entries", changes);
      $("entry-dialog").close();
      await refreshAfterChange();
    } catch (error) {
      if (error.status !== 401) showErrors(error);
    } finally {
      $("save-button").disabled = false;
    }
  }

  async function deleteEditingEntry() {
    const { entry } = state.editing;
    if (!entry) return;
    const message =
      entry.source === "ocr"
        ? "この明細を削除します。LINEで送った領収書の画像も削除され、元に戻せません。よろしいですか？"
        : "この明細を削除します。元に戻せません。よろしいですか？";
    if (!window.confirm(message)) return;
    try {
      await api("DELETE", `/entries/${encodeURIComponent(entry.entry_id)}`);
      $("entry-dialog").close();
      await refreshAfterChange();
    } catch (error) {
      if (error.status !== 401) showErrors(error);
    }
  }

  async function refreshAfterChange() {
    try {
      state.meta = await api("GET", "/meta");
    } catch (_) {
      // 年の一覧の更新に失敗しても、明細の再読み込みは続ける
    }
    await loadYear(state.year);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
