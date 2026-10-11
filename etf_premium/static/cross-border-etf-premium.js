(() => {
  "use strict";

  const AUTO_REFRESH_SECONDS = 30;
  const QUALITY_LABELS = {
    live: "报价时效通过",
    closed: "盘后快照",
    delayed: "行情陈旧",
    inconsistent: "口径异常",
    missing: "数据缺失",
    unknown: "日历待更新",
  };
  const BAND_LABELS = {
    extreme: "极高溢价",
    high: "高溢价",
    elevated: "告警",
    watch: "偏高",
    normal: "平价附近",
    discount: "折价",
    missing: "无法计算",
  };

  const state = {
    payload: null,
    items: [],
    sortKey: null,
    sortDirection: null,
    requestController: null,
    countdown: AUTO_REFRESH_SECONDS,
    timer: null,
  };

  const elements = {};

  function byId(id) {
    return document.getElementById(id);
  }

  function numberOrNull(value) {
    if (value === null || value === undefined || typeof value === "boolean") return null;
    if (typeof value === "string" && !value.trim()) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function formatPrice(value, digits = 3) {
    const number = numberOrNull(value);
    return number === null ? "--" : number.toFixed(digits);
  }

  function formatPct(value, digits = 2) {
    const number = numberOrNull(value);
    if (number === null) return "--";
    const sign = number > 0 ? "+" : "";
    return `${sign}${number.toFixed(digits)}%`;
  }

  function formatAmount(value) {
    const number = numberOrNull(value);
    if (number === null) return "--";
    if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(2)} 亿`;
    if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)} 万`;
    return number.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
  }

  function qualityKey(item) {
    const quality = item?.quality;
    if (typeof quality === "string") return quality;
    return String(item?.qualityStatus || quality?.key || quality?.status || "missing");
  }

  function qualityMessage(item) {
    const quality = item?.quality;
    if (quality && typeof quality === "object") return String(quality.message || quality.reason || "");
    return String(item?.qualityMessage || "");
  }

  function bandKey(item) {
    const band = item?.premiumBand;
    if (typeof band === "string") return band;
    return String(band?.key || band?.status || (numberOrNull(item?.premiumPct) === null ? "missing" : "normal"));
  }

  function benchmarkKey(item) {
    return String(item?.benchmarkKey || item?.benchmark || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  }

  function benchmarkLabel(item) {
    const labels = { nasdaq100: "纳斯达克 100", nasdaqtech: "纳斯达克科技市值加权", sp500: "标普 500" };
    return String(item?.benchmarkLabel || labels[benchmarkKey(item)] || "其他指数");
  }

  function itemIsAlert(item) {
    if (typeof item?.isAlert === "boolean") return item.isAlert;
    if (typeof item?.alert === "boolean") return item.alert;
    const premium = numberOrNull(item?.premiumPct);
    return premium !== null && premium >= 2;
  }

  function toneClass(value) {
    const number = numberOrNull(value);
    if (number === null || number === 0) return "flat";
    return number > 0 ? "positive" : "negative";
  }

  function create(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function summaryCard(label, value, note, tone) {
    const card = create("article", "summary-card");
    if (tone) card.dataset.tone = tone;
    card.append(create("span", "", label), create("strong", "", value), create("small", "", note));
    return card;
  }

  function currentSummary() {
    const summary = state.payload?.summary || {};
    const usableItems = state.items.filter((item) => numberOrNull(item?.premiumPct) !== null);
    const alertItems = usableItems.filter(itemIsAlert);
    const maximum = usableItems.slice().sort((left, right) => Number(right.premiumPct) - Number(left.premiumPct))[0];
    const amounts = state.items.map((item) => numberOrNull(item?.amount)).filter((value) => value !== null);
    return {
      total: numberOrNull(summary.totalCount ?? summary.total) ?? state.items.length,
      usable: numberOrNull(summary.usableCount ?? summary.validCount ?? summary.valid) ?? usableItems.length,
      alerts: numberOrNull(summary.alertCount) ?? alertItems.length,
      maxPremiumPct: numberOrNull(summary.maxPremiumPct ?? summary.maximumPremiumPct ?? maximum?.premiumPct),
      maxPremiumCode: String(summary.maxPremiumCode || summary.maximumPremiumCode || maximum?.code || "--"),
      totalAmount: numberOrNull(summary.totalAmount) ?? (amounts.length ? amounts.reduce((sum, value) => sum + value, 0) : null),
    };
  }

  function renderSummary() {
    const summary = currentSummary();
    const invalid = Math.max(0, summary.total - summary.usable);
    const counts = state.items.reduce((result, item) => {
      const key = benchmarkKey(item);
      result[key] = (result[key] || 0) + 1;
      return result;
    }, {});
    const cards = [
      summaryCard("监测标的", summary.total, `${counts.nasdaq100 || 0} 只纳指100 · ${counts.nasdaqtech || 0} 只科技 · ${counts.sp500 || 0} 只标普`),
      summaryCard("可计算估值", summary.usable, invalid ? `${invalid} 只不可计算；并非均为实时` : "包含盘后或延迟快照", invalid ? "warn" : "good"),
      summaryCard("≥ 2% 告警", summary.alerts, "告警阈值由服务端统一下发", summary.alerts ? "danger" : "good"),
      summaryCard("最高溢价", formatPct(summary.maxPremiumPct), summary.maxPremiumCode, "danger"),
      summaryCard("合计成交额", formatAmount(summary.totalAmount), `当前 ${summary.total} 只 ETF 的已知成交额`),
    ];
    elements.summary.replaceChildren(...cards);
  }

  function filteredItems() {
    const indexFilter = elements.indexFilter.value;
    const bandFilter = elements.bandFilter.value;
    const qualityFilter = elements.qualityFilter.value;
    const query = elements.search.value.trim().toLowerCase();
    const alertOnly = elements.alertOnly.checked;
    return state.items.filter((item) => {
      if (indexFilter !== "all" && benchmarkKey(item) !== indexFilter) return false;
      if (bandFilter !== "all" && bandKey(item) !== bandFilter) return false;
      if (qualityFilter !== "all" && qualityKey(item) !== qualityFilter) return false;
      if (alertOnly && !itemIsAlert(item)) return false;
      if (query) {
        const haystack = `${item?.code || ""} ${item?.name || ""}`.toLowerCase();
        if (!haystack.includes(query)) return false;
      }
      return true;
    });
  }

  function sortValue(item, key) {
    const value = numberOrNull(item?.[key]);
    return value;
  }

  function defaultCompare(left, right) {
    const leftPremium = numberOrNull(left?.premiumPct);
    const rightPremium = numberOrNull(right?.premiumPct);
    if (leftPremium === null && rightPremium !== null) return 1;
    if (leftPremium !== null && rightPremium === null) return -1;
    if (leftPremium !== null && rightPremium !== null && leftPremium !== rightPremium) return rightPremium - leftPremium;
    const amountGap = (numberOrNull(right?.amount) || 0) - (numberOrNull(left?.amount) || 0);
    if (amountGap) return amountGap;
    return String(left?.code || "").localeCompare(String(right?.code || ""), "zh-CN");
  }

  function sortedItems(items) {
    return items
      .map((item, index) => ({ item, index }))
      .sort((leftRow, rightRow) => {
        if (!state.sortKey || !state.sortDirection) {
          const compared = defaultCompare(leftRow.item, rightRow.item);
          return compared || leftRow.index - rightRow.index;
        }
        const left = sortValue(leftRow.item, state.sortKey);
        const right = sortValue(rightRow.item, state.sortKey);
        if (left === null && right !== null) return 1;
        if (left !== null && right === null) return -1;
        if (left === null && right === null) return leftRow.index - rightRow.index;
        const delta = state.sortDirection === "asc" ? left - right : right - left;
        return delta || defaultCompare(leftRow.item, rightRow.item) || leftRow.index - rightRow.index;
      })
      .map((row) => row.item);
  }

  function textCell(text, className) {
    return create("td", className || "", text);
  }

  function renderBenchmarkCell(item) {
    const cell = create("td");
    const key = benchmarkKey(item);
    cell.append(create("span", `benchmark-badge ${key}`, benchmarkLabel(item)));
    return cell;
  }

  function renderFundCell(item) {
    const cell = create("td");
    const box = create("div", "fund-cell");
    const link = create("a", "", item?.name || item?.code || "--");
    link.href = String(item?.sourceUrl || `https://gu.qq.com/${String(item?.secid || "").startsWith("1.") ? "sh" : "sz"}${item?.code || ""}`);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    box.append(link, create("code", "", item?.code || "--"));
    cell.append(box);
    return cell;
  }

  function renderPriceCell(item) {
    const cell = create("td", "numeric");
    cell.append(create("strong", "", formatPrice(item?.marketPrice ?? item?.price, 3)));
    const bid = formatPrice(item?.bidPrice, 3);
    const ask = formatPrice(item?.askPrice, 3);
    cell.append(create("div", "cell-sub", `买 ${bid} / 卖 ${ask}`));
    return cell;
  }

  function renderIopvCell(item) {
    const cell = create("td", "numeric");
    cell.append(create("strong", "", formatPrice(item?.iopv ?? item?.referenceValue, 4)));
    cell.append(create("div", "cell-sub", String(item?.referenceType || "IOPV 参考估值")));
    return cell;
  }

  function renderPremiumCell(item) {
    const band = bandKey(item);
    const cell = create("td");
    cell.append(create("strong", `premium-value premium-${band}`, formatPct(item?.premiumPct)));
    cell.append(create("span", `premium-band premium-${band}`, BAND_LABELS[band] || "--"));
    return cell;
  }

  function renderChangeCell(item) {
    const value = item?.dayChangePct ?? item?.changePct;
    return textCell(formatPct(value), `numeric ${toneClass(value)}`);
  }

  function renderTimeCell(item) {
    const cell = create("td", "time-cell");
    const timestamp = String(item?.quoteTime || item?.asOf || "--");
    const reference = item?.referenceTime ? `IOPV 时点 ${item.referenceTime}` : "IOPV 独立时点：未提供";
    cell.append(create("span", "", timestamp), create("small", "", reference));
    return cell;
  }

  function renderQualityCell(item) {
    const key = qualityKey(item);
    const cell = create("td");
    const badge = create("span", `quality-badge ${key}`, QUALITY_LABELS[key] || key);
    const message = qualityMessage(item);
    if (message) badge.title = message;
    cell.append(badge);
    if (item?.fromCache) cell.append(create("div", "cell-sub", "回退缓存"));
    return cell;
  }

  function renderRows(items) {
    const fragment = document.createDocumentFragment();
    for (const item of items) {
      const row = document.createElement("tr");
      row.dataset.quality = qualityKey(item);
      row.dataset.alert = itemIsAlert(item) ? "true" : "false";
      row.append(
        renderBenchmarkCell(item),
        renderFundCell(item),
        renderPriceCell(item),
        renderIopvCell(item),
        renderPremiumCell(item),
        renderChangeCell(item),
        textCell(formatAmount(item?.amount), "numeric"),
        textCell(formatPct(item?.spreadPct), "numeric"),
        renderTimeCell(item),
        renderQualityCell(item),
      );
      fragment.append(row);
    }
    elements.rows.replaceChildren(fragment);
  }

  function renderLeaders(items) {
    const leaders = items
      .filter((item) => numberOrNull(item?.premiumPct) !== null)
      .sort((left, right) => Number(right.premiumPct) - Number(left.premiumPct))
      .slice(0, 5);
    if (!leaders.length) {
      elements.leaders.replaceChildren(create("p", "empty-state", "当前没有可用的 IOPV 溢价数据。"));
      return;
    }
    const maximum = Math.max(0.01, ...leaders.map((item) => Math.max(0, Number(item.premiumPct))));
    const rows = leaders.map((item) => {
      const row = create("div", "leader-row");
      const label = create("div", "leader-label");
      label.append(create("b", "", item.code || "--"), document.createTextNode(item.name || "--"));
      const track = create("div", "leader-track");
      const bar = create("div", "leader-bar");
      const width = Number(item.premiumPct) <= 0 ? 0 : Math.max(3, (Number(item.premiumPct) / maximum) * 100);
      bar.style.setProperty("--bar-width", `${width.toFixed(2)}%`);
      track.append(bar);
      row.append(label, track, create("div", "leader-value", formatPct(item.premiumPct)));
      return row;
    });
    elements.leaders.replaceChildren(...rows);
  }

  function updateSortHeaders() {
    document.querySelectorAll(".sort-btn").forEach((button) => {
      const key = button.dataset.sort;
      const th = button.closest("th");
      const indicator = button.querySelector("span");
      const active = key === state.sortKey && Boolean(state.sortDirection);
      button.classList.toggle("active", active || (!state.sortKey && key === "premiumPct"));
      indicator.textContent = active ? (state.sortDirection === "asc" ? "↑" : "↓") : (state.sortKey ? "↕" : key === "premiumPct" ? "↓" : "↕");
      th.setAttribute("aria-sort", active ? (state.sortDirection === "asc" ? "ascending" : "descending") : key === "premiumPct" && !state.sortKey ? "descending" : "none");
    });
  }

  function render() {
    renderSummary();
    const filtered = filteredItems();
    const sorted = sortedItems(filtered);
    renderRows(sorted);
    renderLeaders(sorted);
    elements.visibleCount.textContent = `显示 ${sorted.length} / ${state.items.length} 只`;
    elements.empty.hidden = sorted.length > 0;
    updateSortHeaders();
  }

  function setNotice(message, tone = "warn") {
    elements.notice.hidden = !message;
    elements.notice.textContent = message || "";
    elements.notice.dataset.tone = tone;
  }

  function latestReferenceTime(items) {
    const times = items.map((item) => String(item?.referenceTime || "")).filter(Boolean).sort();
    return times[times.length - 1] || "--";
  }

  function applyPayload(payload) {
    state.payload = payload;
    state.items = Array.isArray(payload?.items) ? payload.items.slice() : [];
    elements.updatedAt.textContent = String(payload?.generatedAt || payload?.asOf || "--");
    elements.referenceAt.textContent = "腾讯 IOPV 无独立时点；下表时间仅为报价时间";
    const errors = Array.isArray(payload?.errors) ? payload.errors : [];
    const cacheState = String(payload?.cache?.state || "");
    const usable = state.items.filter((item) => numberOrNull(item?.premiumPct) !== null).length;
    const reason = [...new Set(errors.map((error) => String(error?.error || "")).filter(Boolean))].join("；");
    if (usable === 0) {
      setNotice(`腾讯行情未取得可计算数据（0/${state.items.length}）。${reason || "请检查每行缺失字段或口径异常提示。"} 不使用旧数据冒充本次结果。`, "error");
    } else if (cacheState === "stale") {
      setNotice("上游刷新失败，当前展示最近一次有效缓存；请以每行质量状态和时间为准。", "warn");
    } else if (errors.length) {
      setNotice(`本次可计算 ${usable}/${state.items.length} 只。${reason} 缺失项不会用零值替代。`, "warn");
    } else if (payload?.timing?.marketPhase?.key === "unknown") {
      setNotice("交易日历未覆盖当前年份，所有数值仅作快照参考；请先更新休市日历。", "warn");
    } else if (state.items.some((item) => ["delayed", "inconsistent", "missing"].includes(qualityKey(item)))) {
      setNotice("部分标的数据延迟、缺失或口径异常，请核对每行质量状态；排行不等于交易建议。", "warn");
    } else {
      setNotice("");
    }
    render();
  }

  async function loadData({ force = false } = {}) {
    if (state.requestController) return;
    const controller = new AbortController();
    state.requestController = controller;
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    elements.refreshBtn.disabled = true;
    elements.refreshBtn.textContent = force ? "刷新中…" : "加载中…";
    try {
      const response = await fetch(`/api/cross-border-etf-premium${force ? "?force=1" : ""}`, {
        cache: "no-store",
        signal: controller.signal,
        headers: { Accept: "application/json" },
      });
      if (!(response.headers.get("Content-Type") || "").toLowerCase().includes("application/json")) {
        throw new Error("当前地址未提供 Python 行情接口；请在项目根目录双击 start.bat，再访问终端显示的本机地址");
      }
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
      if (!Array.isArray(payload?.items)) throw new Error("接口未返回 ETF 明细");
      applyPayload(payload);
      state.countdown = AUTO_REFRESH_SECONDS;
    } catch (error) {
      state.payload = null;
      state.items = [];
      elements.updatedAt.textContent = "--";
      elements.referenceAt.textContent = "腾讯 IOPV 无独立时点";
      render();
      const reason = error?.name === "AbortError" ? "请求超时或已取消" : (error?.message || error);
      setNotice(`ETF 溢价数据加载失败：${reason}。已清空旧表，避免误认为当前数据。`, "error");
    } finally {
      window.clearTimeout(timeout);
      state.countdown = AUTO_REFRESH_SECONDS;
      if (state.requestController === controller) state.requestController = null;
      elements.refreshBtn.disabled = false;
      elements.refreshBtn.textContent = "↻ 立即刷新";
    }
  }

  function resetFilters() {
    elements.indexFilter.value = "all";
    elements.bandFilter.value = "all";
    elements.qualityFilter.value = "all";
    elements.search.value = "";
    elements.alertOnly.checked = false;
    state.sortKey = null;
    state.sortDirection = null;
    render();
  }

  function cycleSort(key) {
    if (state.sortKey !== key) {
      state.sortKey = key;
      state.sortDirection = "desc";
    } else if (state.sortDirection === "desc") {
      state.sortDirection = "asc";
    } else {
      state.sortKey = null;
      state.sortDirection = null;
    }
    render();
  }

  function startAutoRefresh() {
    window.clearInterval(state.timer);
    state.timer = window.setInterval(() => {
      if (document.hidden || state.requestController) return;
      state.countdown -= 1;
      if (state.countdown <= 0) {
        state.countdown = AUTO_REFRESH_SECONDS;
        loadData();
      }
      elements.countdown.textContent = `${state.countdown} 秒`;
    }, 1000);
  }

  function bindEvents() {
    elements.refreshBtn.addEventListener("click", () => loadData({ force: true }));
    elements.reset.addEventListener("click", resetFilters);
    [elements.indexFilter, elements.bandFilter, elements.qualityFilter, elements.alertOnly].forEach((element) => {
      element.addEventListener("change", render);
    });
    elements.search.addEventListener("input", render);
    document.querySelectorAll(".sort-btn").forEach((button) => {
      button.addEventListener("click", () => cycleSort(button.dataset.sort));
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        state.countdown = 1;
        elements.countdown.textContent = "1 秒";
      }
    });
    window.addEventListener("beforeunload", () => {
      if (state.requestController) state.requestController.abort();
      window.clearInterval(state.timer);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    Object.assign(elements, {
      summary: byId("etfPremiumSummary"),
      updatedAt: byId("etfPremiumUpdatedAt"),
      countdown: byId("etfPremiumCountdown"),
      refreshBtn: byId("etfPremiumRefreshBtn"),
      notice: byId("etfPremiumNotice"),
      indexFilter: byId("etfPremiumIndexFilter"),
      bandFilter: byId("etfPremiumBandFilter"),
      qualityFilter: byId("etfPremiumQualityFilter"),
      search: byId("etfPremiumSearch"),
      alertOnly: byId("etfPremiumAlertOnly"),
      reset: byId("etfPremiumReset"),
      visibleCount: byId("etfPremiumVisibleCount"),
      referenceAt: byId("etfPremiumReferenceAt"),
      leaders: byId("etfPremiumLeaders"),
      rows: byId("etfPremiumRows"),
      empty: byId("etfPremiumEmpty"),
    });
    const startupHint = byId("etfPremiumStartupHint");
    if (window.location.protocol === "file:") {
      if (startupHint) startupHint.hidden = true;
      setNotice("当前只是 HTML 文件预览，未连接行情服务。请返回项目根目录双击 start.bat，或运行 python run.py，再访问终端显示的本机地址（默认 http://127.0.0.1:8765/）。", "warn");
      elements.countdown.textContent = "未启动";
      elements.refreshBtn.disabled = true;
      elements.refreshBtn.textContent = "请先启动本地服务";
      const row = create("tr");
      const cell = create("td", "empty-state", "本地服务未启动，暂无行情；HTML 文件不能独立查询行情。");
      cell.colSpan = 10;
      row.append(cell);
      elements.rows.replaceChildren(row);
      elements.leaders.replaceChildren(create("p", "empty-state", "请通过本地 Python 服务打开页面。"));
      return;
    }
    if (startupHint) startupHint.hidden = true;
    bindEvents();
    startAutoRefresh();
    loadData();
  });
})();
