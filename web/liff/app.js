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
    providers: [],
    providerGroups: [],
    tab: "entries",
    memberFilter: "",
    providerFilter: "",
    issuesOnly: false,
    focusGroupId: null,
    editing: null, // { entry, initial } 新規追加なら entry は null
    renaming: null, // { name, sample } 支払先の名前をまとめて直すダイアログ
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
      const [list, checks, providers] = await Promise.all([
        api("GET", `/entries?year=${year}`),
        api("GET", `/checks?year=${year}`),
        api("GET", `/providers?year=${year}`),
      ]);
      state.entries = list.entries;
      state.summary = list.summary;
      state.issues = checks.issues;
      state.readyForExport = checks.ready_for_export;
      state.providers = providers.providers;
      state.providerGroups = providers.groups;
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
    $("tab-providers").addEventListener("click", () => switchTab("providers"));
    $("provider-filter-clear").addEventListener("click", () => {
      state.providerFilter = "";
      renderEntries();
    });
    $("receipt-image-button").addEventListener("click", showReceiptImage);
    const providerInput = $("entry-form").elements.provider_name;
    const onProviderInput = () => {
      updateProviderSuggestion();
      updateBulkRenameOption();
    };
    providerInput.addEventListener("input", onProviderInput);
    providerInput.addEventListener("change", onProviderInput);
    $("provider-suggestion-apply").addEventListener("click", () => {
      providerInput.value = $("provider-suggestion").dataset.name || providerInput.value;
      onProviderInput();
    });
    $("rename-cancel").addEventListener("click", () => $("rename-dialog").close());
    $("rename-form").addEventListener("submit", (event) => {
      event.preventDefault();
      submitRename();
    });
    $("rename-image-button").addEventListener("click", () => {
      const sample = state.renaming && state.renaming.sample;
      if (sample) loadReceiptImage(sample, $("rename-image"), $("rename-image-message"), $("rename-image-button"));
    });
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
    renderProviderOptions();
  }

  function renderProviderOptions() {
    $("provider-options").replaceChildren(...(state.meta.provider_names || []).map((name) => el("option", { value: name })));
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
    renderProviders();
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
      if (state.providerFilter && entry.provider_name !== state.providerFilter) return false;
      if (state.issuesOnly && !byEntry.has(entry.entry_id)) return false;
      return true;
    });
    $("entry-list").replaceChildren(...visible.map((entry) => renderEntry(entry, byEntry.get(entry.entry_id) || [])));
    $("provider-filter-chip").hidden = !state.providerFilter;
    $("provider-filter-label").textContent = `支払先: ${state.providerFilter}`;
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
        if (issue.group_id) {
          actions.push(el("button", { type: "button", text: "支払先タブで確認", onclick: () => openProviderGroup(issue.group_id) }));
        }
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
    for (const name of ["entries", "checks", "providers"]) {
      $(`tab-${name}`).setAttribute("aria-selected", String(name === tab));
      $(`panel-${name}`).hidden = name !== tab;
    }
  }

  // ---------- 支払先（表記ゆれ・読み取りミス） ----------
  function renderProviders() {
    const groups = state.providerGroups;
    const badge = $("providers-badge");
    badge.hidden = groups.length === 0;
    badge.textContent = String(groups.length);
    $("groups-lead").textContent = groups.length
      ? "同じ支払先なら、正しい名前を選んでそろえてください。そろえた名前は、次回からLINEで読み取ったときにも使われます。"
      : "表記ゆれの候補はありません。";
    $("group-list").replaceChildren(...groups.map(renderGroup));
    $("provider-list").replaceChildren(
      ...state.providers.map((provider) =>
        el("li", { className: "provider-item" }, [
          el(
            "button",
            {
              type: "button",
              className: `provider-row${provider.suspicious_reason ? " suspicious" : ""}`,
              onclick: () => {
                state.providerFilter = provider.name;
                renderEntries();
                switchTab("entries");
              },
            },
            [
              el("span", { className: "variant-name", text: provider.name }),
              el("span", { className: "entry-amount", text: formatYen(provider.total_amount_yen) }),
              el("span", {
                className: "variant-stat",
                text: `${provider.count}件${provider.suspicious_reason ? `　⚠ ${provider.suspicious_reason}` : ""}`,
              }),
            ]
          ),
          el("button", {
            type: "button",
            className: "provider-edit",
            text: "名前を直す",
            "aria-label": `「${provider.name}」の名前をまとめて直す`,
            onclick: () => openRenameDialog(provider.name),
          }),
        ])
      )
    );
    if (state.focusGroupId) {
      const target = document.getElementById(`group-${state.focusGroupId}`);
      state.focusGroupId = null;
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function renderGroup(group) {
    const radioName = `target-${group.group_id}`;
    const rows = group.names.map((stat, index) =>
      el("div", { className: "variant-row" }, [
        el("input", {
          type: "radio",
          name: radioName,
          value: stat.name,
          id: `${radioName}-${index}`,
          checked: stat.name === group.suggested_name,
        }),
        el("label", { className: "variant-name", for: `${radioName}-${index}`, text: stat.name }),
        el("label", { className: "variant-distinct" }, [
          el("input", { type: "checkbox", "data-distinct": stat.name }),
          "別の支払先",
        ]),
        el("span", {
          className: "variant-stat",
          text: `${stat.count}件　${formatYen(stat.total_amount_yen)}${stat.suspicious_reason ? `　⚠ ${stat.suspicious_reason}` : ""}`,
        }),
      ])
    );
    const customInput = el("input", { type: "text", className: "custom-name", maxlength: 100, placeholder: "正しい名前を入力", hidden: true });
    const customRadio = el("input", { type: "radio", name: radioName, value: "", id: `${radioName}-custom` });
    customRadio.addEventListener("change", () => {
      customInput.hidden = false;
      customInput.focus();
    });
    const customRow = el("div", { className: "variant-row" }, [
      customRadio,
      el("label", { className: "variant-name", for: `${radioName}-custom`, text: "別の名前にする" }),
      customInput,
    ]);
    const message = el("p", { className: "form-error", role: "alert" });
    const fieldset = el("fieldset", { className: "group-card", id: `group-${group.group_id}` }, [
      el("legend", { text: "同じ支払先ですか？" }),
      ...rows,
      customRow,
      message,
    ]);
    const actions = el("div", { className: "group-actions" }, [
      el("button", { type: "button", className: "primary", text: "選んだ名前にそろえる", onclick: () => unifyGroup(group, fieldset, customInput, message) }),
      el("button", { type: "button", text: "すべて別の支払先", onclick: () => markGroupDistinct(group, message) }),
    ]);
    fieldset.append(actions);
    return fieldset;
  }

  async function unifyGroup(group, fieldset, customInput, message) {
    message.textContent = "";
    const selected = fieldset.querySelector(`input[type="radio"]:checked`);
    const target = selected && selected.value ? selected.value : customInput.value.trim();
    if (!target) {
      message.textContent = "そろえる名前を選ぶか入力してください。";
      return;
    }
    const distinct = [...fieldset.querySelectorAll("input[data-distinct]:checked")]
      .map((input) => input.dataset.distinct)
      .filter((name) => name !== target);
    const fromNames = group.names.map((stat) => stat.name).filter((name) => name !== target && !distinct.includes(name));
    if (!fromNames.length && !distinct.length) {
      message.textContent = "変更する名前がありません。";
      return;
    }
    const confirmText = fromNames.length
      ? `「${fromNames.join("」「")}」の明細（ほかの年も含む）を「${target}」に変更します。次回からLINEで読み取った同じ名前も自動でそろえます。よろしいですか？`
      : "チェックした名前を別の支払先として記録します。よろしいですか？";
    if (!window.confirm(confirmText)) return;
    try {
      if (fromNames.length) await api("POST", "/providers/rename", { from_names: fromNames, to_name: target });
      if (distinct.length) await api("POST", "/providers/distinct", { names: [target, ...distinct] });
      await refreshAfterChange();
    } catch (error) {
      if (error.status !== 401) message.textContent = errorText(error);
    }
  }

  async function markGroupDistinct(group, message) {
    message.textContent = "";
    if (!window.confirm("これらはすべて別の支払先として記録し、今後は候補に出しません。よろしいですか？")) return;
    try {
      await api("POST", "/providers/distinct", { names: group.names.map((stat) => stat.name) });
      await refreshAfterChange();
    } catch (error) {
      if (error.status !== 401) message.textContent = errorText(error);
    }
  }

  function openProviderGroup(groupId) {
    state.focusGroupId = groupId;
    switchTab("providers");
    renderProviders();
  }

  function errorText(error) {
    const fields = (error.body && error.body.details && error.body.details.fields) || {};
    const first = Object.values(fields)[0];
    return first || error.message;
  }

  // 入力中の支払先が既存の名前と似ていたら、そろえるよう提案する（判定はサーバーと同じ考え方の簡易版）
  const CORPORATE_WORDS = [
    "社会医療法人社団", "社会医療法人財団", "社会医療法人", "医療法人社団", "医療法人財団", "医療法人",
    "一般社団法人", "一般財団法人", "公益社団法人", "公益財団法人", "社会福祉法人", "独立行政法人",
    "株式会社", "有限会社", "合同会社", "(株)", "(有)", "(医)",
  ];
  const CONFUSABLES = { "口": "ロ", "力": "カ", "工": "エ", "一": "ー", "―": "ー", "‐": "ー", "-": "ー", "卜": "ト", "二": "ニ", "八": "ハ", "夕": "タ" };

  function providerKey(name) {
    let text = String(name || "").normalize("NFKC").toLowerCase();
    for (const word of CORPORATE_WORDS) text = text.split(word.toLowerCase()).join("");
    text = text.replace(/[\s・･.,、。"'`]+/g, "");
    return [...text].map((ch) => CONFUSABLES[ch] || ch).join("");
  }

  function similarity(a, b) {
    if (a === b) return 1;
    const [shorter, longer] = a.length <= b.length ? [a, b] : [b, a];
    if (shorter.length >= 4 && longer.includes(shorter)) return 0.9;
    const prev = Array.from({ length: b.length + 1 }, (_, i) => i);
    for (let i = 1; i <= a.length; i += 1) {
      let diagonal = prev[0];
      prev[0] = i;
      for (let j = 1; j <= b.length; j += 1) {
        const above = prev[j];
        prev[j] = Math.min(prev[j] + 1, prev[j - 1] + 1, diagonal + (a[i - 1] === b[j - 1] ? 0 : 1));
        diagonal = above;
      }
    }
    return 1 - prev[b.length] / Math.max(a.length, b.length, 1);
  }

  function updateProviderSuggestion() {
    const value = $("entry-form").elements.provider_name.value.trim();
    const box = $("provider-suggestion");
    const names = state.meta.provider_names || [];
    box.hidden = true;
    if (!value || names.includes(value)) return;
    const key = providerKey(value);
    if (key.length < 2) return;
    let best = null;
    let bestScore = 0;
    for (const name of names) {
      const score = similarity(key, providerKey(name));
      if (score > bestScore) {
        best = name;
        bestScore = score;
      }
    }
    if (!best || bestScore < 0.8) return;
    box.dataset.name = best;
    $("provider-suggestion-text").textContent = `「${best}」と似ています。`;
    box.hidden = false;
  }

  // ---------- 領収書の画像 ----------
  function showReceiptImage() {
    const { entry } = state.editing || {};
    if (entry) loadReceiptImage(entry, $("receipt-image"), $("receipt-image-message"), $("receipt-image-button"));
  }

  async function loadReceiptImage(entry, image, message, button) {
    message.textContent = "画像を読み込んでいます…";
    button.disabled = true;
    try {
      const body = await api("GET", `/entries/${encodeURIComponent(entry.entry_id)}/image`);
      image.onload = () => {
        message.textContent = "";
      };
      image.onerror = () => {
        image.hidden = true;
        message.textContent = "画像を表示できませんでした。もう一度お試しください。";
        button.disabled = false;
      };
      image.src = body.url;
      image.hidden = false;
    } catch (error) {
      if (error.status !== 401) message.textContent = error.message;
      button.disabled = false;
    }
  }

  function resetImage(image, message, button) {
    image.hidden = true;
    image.removeAttribute("src");
    message.textContent = "";
    button.disabled = false;
  }

  // ---------- 支払先の名前をまとめて直す ----------
  function entriesWithProvider(name) {
    return state.entries.filter((entry) => entry.provider_name === name);
  }

  function openRenameDialog(name) {
    const form = $("rename-form");
    state.renaming = { name };
    form.elements.to_name.value = name;
    $("rename-error").textContent = "";
    const count = entriesWithProvider(name).length;
    $("rename-hint").textContent =
      `「${name}」の明細（この年 ${count}件）を、入力した名前に変更します。` +
      "ほかの年の同じ名前の明細も変更され、次回からLINEで読み取った同じ名前も自動で直します。";

    const others = state.providers.filter((provider) => provider.name !== name);
    $("rename-merge-fieldset").hidden = others.length === 0;
    $("rename-merge-options").replaceChildren(
      ...others.map((provider, index) =>
        el("label", { className: "merge-option", for: `merge-${index}` }, [
          el("input", { type: "checkbox", id: `merge-${index}`, "data-merge": provider.name }),
          el("span", { text: provider.name }),
          el("small", { text: `${provider.count}件` }),
        ])
      )
    );

    const sample = entriesWithProvider(name).find((entry) => entry.source === "ocr");
    state.renaming.sample = sample || null;
    $("rename-image-area").hidden = !sample;
    resetImage($("rename-image"), $("rename-image-message"), $("rename-image-button"));
    $("rename-save").disabled = false;
    $("rename-dialog").showModal();
  }

  async function submitRename() {
    const { name } = state.renaming || {};
    if (!name) return;
    const target = $("rename-form").elements.to_name.value.trim();
    const merged = [...document.querySelectorAll("#rename-merge-options input[data-merge]:checked")].map(
      (input) => input.dataset.merge
    );
    const fromNames = [name, ...merged].filter((value, index, list) => value !== target && list.indexOf(value) === index);
    if (!target) {
      $("rename-error").textContent = "正しい名前を入力してください。";
      return;
    }
    if (!fromNames.length) {
      $("rename-error").textContent = "名前が変わっていません。";
      return;
    }
    if (!window.confirm(`「${fromNames.join("」「")}」の明細（ほかの年も含む）を「${target}」に変更します。よろしいですか？`)) return;
    $("rename-save").disabled = true;
    try {
      const body = await api("POST", "/providers/rename", { from_names: fromNames, to_name: target });
      $("rename-dialog").close();
      await refreshAfterChange();
      setStatus(`${body.updated}件の明細を「${target}」に変更しました。`);
    } catch (error) {
      if (error.status !== 401) $("rename-error").textContent = errorText(error);
    } finally {
      $("rename-save").disabled = false;
    }
  }

  // 編集画面で支払先の名前を変えたとき、同じ名前のほかの明細もまとめて直せるようにする
  function updateBulkRenameOption() {
    const { entry, initial } = state.editing || {};
    const row = $("bulk-rename-row");
    const value = $("entry-form").elements.provider_name.value.trim();
    const oldName = initial ? initial.provider_name : "";
    if (!entry || !oldName || !value || value === oldName) {
      row.hidden = true;
      return;
    }
    const count = entriesWithProvider(oldName).length;
    const wasHidden = row.hidden;
    $("bulk-rename-label").textContent =
      `「${oldName}」の明細をすべてこの名前に変更する（この年 ${count}件。ほかの年の同じ名前の明細と、次回からの読み取り結果にも反映）`;
    if (wasHidden) $("bulk-rename").checked = count >= 2;
    row.hidden = false;
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
    $("receipt-image-area").hidden = !(entry && entry.source === "ocr");
    resetImage($("receipt-image"), $("receipt-image-message"), $("receipt-image-button"));
    $("bulk-rename-row").hidden = true;
    $("bulk-rename").checked = false;
    updateProviderSuggestion();
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
    const bulkRename = Boolean(entry && changes.provider_name !== undefined && !$("bulk-rename-row").hidden && $("bulk-rename").checked);
    $("save-button").disabled = true;
    try {
      let renamed = null;
      if (bulkRename) {
        const { provider_name: newName, ...rest } = changes;
        if (Object.keys(rest).length) await api("PATCH", `/entries/${encodeURIComponent(entry.entry_id)}`, rest);
        renamed = await api("POST", "/providers/rename", {
          from_names: [state.editing.initial.provider_name],
          to_name: newName.trim(),
        });
      } else if (entry) {
        await api("PATCH", `/entries/${encodeURIComponent(entry.entry_id)}`, changes);
      } else {
        await api("POST", "/entries", changes);
      }
      $("entry-dialog").close();
      await refreshAfterChange();
      if (renamed) setStatus(`${renamed.updated}件の明細の支払先を変更しました。`);
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
      renderProviderOptions();
    } catch (_) {
      // 年の一覧の更新に失敗しても、明細の再読み込みは続ける
    }
    await loadYear(state.year);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
