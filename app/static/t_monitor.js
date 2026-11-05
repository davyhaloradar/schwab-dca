/* 5-minute VWAP 做T chart + trades table. SSE first, poll if the stream drops. */
(function () {
  const bootEl = document.getElementById("t-boot");
  if (!bootEl) return;
  const boot = JSON.parse(bootEl.textContent || "{}");

  const chartHost = document.getElementById("t-chart");
  const emptyEl = document.getElementById("t-chart-empty");
  const statusEl = document.getElementById("t-status");
  const posEl = document.getElementById("t-positions");
  const tbody = document.getElementById("t-trades");
  const noteEl = document.getElementById("t-table-note");
  const streamBadge = document.getElementById("t-stream-state");
  const errBox = document.getElementById("t-errors");
  const tabs = document.getElementById("t-symbol-tabs");

  let active = (boot.symbols && boot.symbols[0]) || "";
  let chart = null;
  let candle = null;
  let vwap = null;
  let entryLine = null;
  let exitLine = null;
  let stopLine = null;
  let lastKey = "";
  let pollTimer = null;
  let es = null;
  let viewMode = "live";
  let replayDate = "";
  let replayRange = "1m";
  let replayToken = 0;

  function parseIsoDate(text) {
    var parts = String(text || "").split("-");
    if (parts.length < 3) return null;
    return new Date(Date.UTC(+parts[0], +parts[1] - 1, +parts[2]));
  }

  function formatIsoDate(dt) {
    var m = dt.getUTCMonth() + 1;
    var d = dt.getUTCDate();
    return dt.getUTCFullYear() + "-" + (m < 10 ? "0" : "") + m + "-" + (d < 10 ? "0" : "") + d;
  }

  function addUtcMonths(dt, months) {
    var index = dt.getUTCMonth() + months;
    var year = dt.getUTCFullYear() + Math.floor(index / 12);
    var month = ((index % 12) + 12) % 12;
    var last = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
    var day = Math.min(dt.getUTCDate(), last);
    return new Date(Date.UTC(year, month, day));
  }

  function todayEt(snap) {
    var raw = (snap && snap.now_et) || "";
    var match = /^(\d{4}-\d{2}-\d{2})/.exec(raw);
    if (match) return match[1];
    return formatIsoDate(new Date());
  }

  function rangeStartIso(today, preset) {
    var dt = parseIsoDate(today);
    if (!dt) return today;
    if (preset === "1w") return formatIsoDate(new Date(dt.getTime() - 7 * 86400000));
    var months = { "1m": -1, "3m": -3, "6m": -6, "1y": -12 }[preset];
    if (months === undefined) return "0000-01-01";
    return formatIsoDate(addUtcMonths(dt, months));
  }

  function datesInRange(dates, snap) {
    var today = todayEt(snap);
    var start = rangeStartIso(today, replayRange);
    return (dates || []).filter(function (day) { return day >= start && day <= today; });
  }

  function num(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function fmtPx(v) {
    const n = num(v);
    if (n === null) return "—";
    return n.toFixed(4);
  }

  function fmtPnl(v) {
    if (v === null || v === undefined || v === "") return "—";
    const n = num(v);
    if (n === null) return "—";
    return (n > 0 ? "+" : "") + n.toFixed(2);
  }

  function phaseLabel(phase, isToday) {
    if (!isToday) return "最近交易日（今日 K 线尚未生成，不补写模拟成交）";
    if (phase === "rth") return "常规交易时段";
    if (phase === "pre") return "盘前";
    if (phase === "post") return "盘后";
    return "休市";
  }

  function setBadge(text, live) {
    if (!streamBadge) return;
    streamBadge.textContent = text;
    streamBadge.className = "badge " + (live ? "badge-ok" : "badge-dry");
  }

  function chartBoxHeight() {
    if (!chartHost) return 440;
    if (window.matchMedia && window.matchMedia("(max-width: 720px)").matches) return 320;
    return 440;
  }

  function lockChartBox() {
    if (!chartHost) return;
    var height = chartBoxHeight();
    chartHost.hidden = false;
    chartHost.style.display = "block";
    chartHost.style.width = "100%";
    chartHost.style.minHeight = (height - 40) + "px";
    chartHost.style.height = height + "px";
  }

  function resizeChart() {
    if (!chart || !chartHost) return;
    lockChartBox();
    var width = chartHost.clientWidth || chartHost.offsetWidth || 640;
    var height = chartHost.clientHeight || chartBoxHeight();
    if (width < 20) width = 640;
    if (height < 20) height = chartBoxHeight();
    chart.resize(width, height);
  }

  function showChartNote(text) {
    if (!emptyEl) return;
    emptyEl.hidden = !text;
    if (text) emptyEl.textContent = text;
  }

  function ensureChart() {
    if (chart) return true;
    if (!chartHost || !window.LightweightCharts) return false;
    lockChartBox();
    var width = chartHost.clientWidth || chartHost.offsetWidth || 640;
    var height = chartHost.clientHeight || chartBoxHeight();
    if (width < 20) width = 640;
    if (height < 20) height = chartBoxHeight();
    chart = LightweightCharts.createChart(chartHost, {
      autoSize: false,
      width: width,
      height: height,
      layout: {
        background: { type: "solid", color: "#0b0f14" },
        textColor: "#8b9bb4",
      },
      grid: {
        vertLines: { color: "#1e293b" },
        horzLines: { color: "#1e293b" },
      },
      rightPriceScale: { borderColor: "#2d3a4f" },
      timeScale: {
        borderColor: "#2d3a4f",
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });
    candle = chart.addCandlestickSeries({
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderUpColor: "#22c55e",
      borderDownColor: "#ef4444",
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
      priceLineVisible: false,
      lastValueVisible: false,
    });
    vwap = chart.addLineSeries({
      color: "#f59e0b",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
      title: "VWAP",
    });
    var dashed = (LightweightCharts.LineStyle && LightweightCharts.LineStyle.Dashed) || 2;
    var levelOpts = {
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: true,
    };
    entryLine = chart.addLineSeries(Object.assign({}, levelOpts, {
      color: "#38bdf8",
      title: "买入",
    }));
    exitLine = chart.addLineSeries(Object.assign({}, levelOpts, {
      color: "#c4b5fd",
      lineStyle: dashed,
      title: "出场",
    }));
    stopLine = chart.addLineSeries(Object.assign({}, levelOpts, {
      color: "#f87171",
      lineStyle: dashed,
      title: "止损",
    }));
    return true;
  }

  function seriesOf(snap) {
    var series = (snap && snap.series) || {};
    if (active && series[active]) return series[active];
    if (active) {
      var up = String(active).toUpperCase();
      if (series[up]) {
        active = up;
        return series[up];
      }
    }
    var keys = Object.keys(series);
    if (keys.length === 1) {
      active = keys[0];
      return series[keys[0]];
    }
    return null;
  }

  function levelValue(bar, field, vwap, bps) {
    var direct = num(bar[field]);
    if (direct !== null) return direct;
    if (vwap === null || bps === null) return null;
    return Math.round(vwap * (1 - bps / 10000) * 10000) / 10000;
  }

  function barsToSeries(bars, params) {
    var seen = {};
    var candles = [];
    var line = [];
    var entry = [];
    var exit = [];
    var stop = [];
    var entryBps = params ? num(params.entry_bps) : null;
    var exitBps = params ? num(params.exit_bps) : null;
    var stopBps = params ? num(params.stop_bps) : null;
    if (exitBps === null) exitBps = 5;
    bars.forEach(function (b) {
      var t = num(b.time);
      if (t === null || seen[t]) return;
      var open = num(b.open);
      var high = num(b.high);
      var low = num(b.low);
      var close = num(b.close);
      if (open === null || high === null || low === null || close === null) return;
      seen[t] = true;
      candles.push({ time: t, open: open, high: high, low: low, close: close });
      var v = num(b.vwap);
      if (v !== null) line.push({ time: t, value: v });
      var e = levelValue(b, "entry", v, entryBps);
      var x = levelValue(b, "exit", v, exitBps);
      var s = levelValue(b, "stop", v, stopBps);
      if (e !== null) entry.push({ time: t, value: e });
      if (x !== null) exit.push({ time: t, value: x });
      if (s !== null) stop.push({ time: t, value: s });
    });
    function byTime(a, b) { return a.time - b.time; }
    candles.sort(byTime);
    line.sort(byTime);
    entry.sort(byTime);
    exit.sort(byTime);
    stop.sort(byTime);
    return { candles: candles, line: line, entry: entry, exit: exit, stop: stop, seen: seen };
  }

  function paintLevel(series, points, replace) {
    if (!series) return;
    if (!points.length) {
      series.setData([]);
      return;
    }
    if (replace) series.setData(points);
    else series.update(points[points.length - 1]);
  }

  function paintChart(snap) {
    var row = seriesOf(snap);
    var bars = (row && row.bars) || [];
    lockChartBox();
    if (!window.LightweightCharts) {
      showChartNote("图表库未加载。请刷新；成交表仍会更新。");
      return;
    }
    if (!ensureChart() || !candle || !vwap) {
      showChartNote("图表初始化失败。请刷新页面。");
      return;
    }
    if (!bars.length) {
      showChartNote("暂无 5 分钟 K 线。开盘后会显示最近一个交易日。");
      candle.setData([]);
      vwap.setData([]);
      paintLevel(entryLine, [], true);
      paintLevel(exitLine, [], true);
      paintLevel(stopLine, [], true);
      candle.setMarkers([]);
      lastKey = active + ":0";
      resizeChart();
      return;
    }
    showChartNote("");
    var built = barsToSeries(bars, (snap.params && snap.params[active]) || null);
    var key = active + ":" + ((row && row.session_date) || "") + ":" + built.candles.length;
    var replace = key !== lastKey;
    if (replace) {
      candle.setData(built.candles);
      vwap.setData(built.line);
      lastKey = key;
      resizeChart();
      chart.timeScale().fitContent();
    } else if (built.candles.length) {
      candle.update(built.candles[built.candles.length - 1]);
      if (built.line.length) vwap.update(built.line[built.line.length - 1]);
      resizeChart();
    }
    paintLevel(entryLine, built.entry, replace);
    paintLevel(exitLine, built.exit, replace);
    paintLevel(stopLine, built.stop, replace);
    var markers = [];
    (snap.trades || []).forEach(function (t) {
      if (!t || t.symbol !== active || !built.seen[t.time]) return;
      var buy = t.side === "BUY";
      markers.push({
        time: t.time,
        position: buy ? "belowBar" : "aboveBar",
        color: buy ? "#22c55e" : "#f59e0b",
        shape: buy ? "arrowUp" : "arrowDown",
        text: buy ? "买" : "卖",
      });
    });
    candle.setMarkers(markers);
  }

  function paintStatus(snap) {
    const row = seriesOf(snap);
    const running = snap.engine_running ? "监控运行中" : "监控已停止";
    const phase = phaseLabel(snap.market_phase, row && row.is_today);
    const src = (row && row.source) || "—";
    const session = (row && row.session_date) || "—";
    const q = (row && row.quote) || {};
    const bars = (row && row.bars) || [];
    const lastVwap = bars.length ? bars[bars.length - 1].vwap : null;
    const px = q.mid || q.last || (bars.length ? bars[bars.length - 1].close : null);
    if (statusEl) {
      statusEl.textContent =
        running +
        " · " + phase +
        " · " + active +
        " 会话 " + session +
        " · 现价 " + fmtPx(px) +
        " · VWAP " + fmtPx(lastVwap) +
        " · 数据 " + src +
        (snap.now_et ? " · " + snap.now_et : "");
    }
    if (posEl) {
      posEl.textContent = "";
      (snap.positions || []).forEach(function (p) {
        const chip = document.createElement("span");
        chip.className = "t-pos";
        const pnl = p.unrealized;
        const pnlTxt = pnl === null || pnl === undefined ? "" : " · 浮动 " + fmtPnl(pnl);
        chip.textContent = p.symbol + " 持仓 " + p.qty + " 股 @ " + fmtPx(p.entry_px) + "（" + p.entry_et + "）" + pnlTxt;
        posEl.appendChild(chip);
      });
    }
    if (noteEl) {
      if (snap.engine_running) {
        noteEl.textContent = "监控运行中。新的模拟成交会直接出现在下表。";
      } else {
        noteEl.textContent = "监控已停止，表里是已保存的模拟成交。点「启动监控」后才会补记或继续。";
      }
    }
    if (errBox) {
      errBox.textContent = "";
      (snap.errors || []).forEach(function (err) {
        const p = document.createElement("p");
        p.className = "banner live";
        p.textContent = err;
        errBox.appendChild(p);
      });
    }
  }

  function paintTable(snap) {
    if (!tbody) return;
    const rows = (snap.trades || []).slice().reverse();
    tbody.textContent = "";
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 7;
      td.className = "empty";
      td.textContent = snap.table_empty
        || (snap.engine_running
          ? "尚无模拟成交。价格跌破 VWAP 入场阈值后会写入这里。"
          : "尚无模拟成交。");
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    rows.forEach(function (t) {
      const tr = document.createElement("tr");
      const time = document.createElement("td");
      const cn = document.createElement("div");
      cn.className = "ts-china";
      cn.textContent = t.time_cn || "—";
      const et = document.createElement("div");
      et.className = "muted ts-utc";
      et.textContent = t.time_et || "";
      time.appendChild(cn);
      time.appendChild(et);

      const sym = document.createElement("td");
      sym.textContent = t.symbol;

      const side = document.createElement("td");
      const badge = document.createElement("span");
      const buy = t.side === "BUY";
      badge.className = "badge " + (buy ? "badge-buy" : "badge-sell");
      badge.textContent = buy ? "买入" : "卖出";
      side.appendChild(badge);

      const qty = document.createElement("td");
      qty.textContent = String(t.qty);

      const px = document.createElement("td");
      px.className = "price-cell";
      px.textContent = fmtPx(t.price);
      const src = document.createElement("div");
      src.className = "muted ts-utc";
      src.textContent = t.price_source_label || "";
      px.appendChild(src);

      const pnl = document.createElement("td");
      pnl.textContent = t.side === "SELL" ? fmtPnl(t.pnl) : "持仓中";
      if (t.side === "SELL" && num(t.pnl) !== null) {
        pnl.className = num(t.pnl) >= 0 ? "pnl-pos" : "pnl-neg";
      } else {
        pnl.className = "muted";
      }

      const reason = document.createElement("td");
      reason.textContent = t.reason_label || t.reason || "";

      [time, sym, side, qty, px, pnl, reason].forEach(function (td) { tr.appendChild(td); });
      tbody.appendChild(tr);
    });
  }

  function apply(snap) {
    if (!snap) return;
    boot.__last = snap;
    if (!active && snap.symbols && snap.symbols.length) active = snap.symbols[0];
    if (snap.symbols && snap.symbols.length === 1) active = snap.symbols[0];
    if (snap.symbols && snap.symbols.length && snap.symbols.indexOf(active) < 0) {
      active = snap.symbols[0];
      lastKey = "";
    }
    var replayDateChanged = false;
    if (viewMode !== "replay") replayDateChanged = refreshReplayDates(snap);
    syncClearSymbol();
    if (viewMode === "replay") {
      if (replayDateChanged) loadReplay();
      return;
    }
    try {
      paintChart(snap);
    } catch (err) {
      lastKey = "";
      showChartNote("图表绘制失败：" + (err && err.message ? err.message : "未知错误"));
    }
    paintStatus(snap);
    paintTable(snap);
  }

  function refreshReplayDates(snap) {
    var sel = document.getElementById("t-replay-date");
    if (!sel) return false;
    var before = replayDate;
    var dates = datesInRange((snap && snap.sessions && snap.sessions[active]) || [], snap);
    var prev = replayDate || sel.value;
    sel.textContent = "";
    if (!dates.length) {
      var empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "该范围内暂无交易日";
      sel.appendChild(empty);
      replayDate = "";
      return replayDate !== before;
    }
    dates.forEach(function (d) {
      var opt = document.createElement("option");
      opt.value = d;
      opt.textContent = d;
      sel.appendChild(opt);
    });
    sel.value = dates.indexOf(prev) >= 0 ? prev : dates[0];
    replayDate = sel.value;
    return replayDate !== before;
  }

  function loadReplay() {
    if (viewMode !== "replay") return;
    var date = replayDate;
    var symbol = active;
    if (!date || !symbol || !boot.replay_url) {
      showChartNote("该范围内暂无缓存的交易日。");
      if (noteEl) noteEl.textContent = "回看模式：当前范围里没有可选交易日，不会补写成交。";
      paintTable({ trades: [], engine_running: false, table_empty: "该范围内没有可选交易日。" });
      return;
    }
    var token = ++replayToken;
    var url = boot.replay_url
      + "?symbol=" + encodeURIComponent(symbol)
      + "&date=" + encodeURIComponent(date);
    fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (payload) {
        if (token !== replayToken || viewMode !== "replay" || symbol !== active) return;
        var view = {
          series: {},
          trades: payload.trades || [],
          symbols: [symbol],
          engine_running: false,
          table_empty: "该日没有已保存的模拟成交。回看不会补写新成交。",
        };
        view.series[symbol] = {
          symbol: symbol,
          session_date: payload.session_date || date,
          is_today: false,
          source: payload.source,
          bars: payload.bars || [],
          quote: null,
        };
        lastKey = "";
        try {
          paintChart(view);
        } catch (err) {
          showChartNote("图表绘制失败：" + (err && err.message ? err.message : "未知错误"));
        }
        if (payload.error) showChartNote(payload.error);
        if (statusEl) {
          statusEl.textContent = "回看 · " + symbol + " · " + (payload.session_date || date)
            + " · 只看行情 · 已保存成交 " + (payload.trades || []).length + " 笔"
            + (payload.source ? " · " + payload.source : "")
            + (payload.error ? " · " + payload.error : "");
        }
        if (posEl) posEl.textContent = "";
        if (noteEl) noteEl.textContent = "指定天：下表只列出所选交易日已保存的模拟成交，不会补写。";
        paintTable(view);
      })
      .catch(function () {
        if (token === replayToken) showChartNote("回看行情加载失败。");
      });
  }

  function syncReplayControls() {
    var wrap = document.getElementById("t-replay-day-wrap");
    var sel = document.getElementById("t-replay-date");
    var showDay = viewMode === "replay" && (replayRange || "1m") === "day";
    if (wrap) wrap.hidden = !showDay;
    if (sel) sel.disabled = !showDay;
  }

  function paintContinuous(symbol, label, payload) {
    var bars = payload.bars || [];
    var trades = payload.trades || [];
    var days = (payload.sessions || []).length;
    var view = {
      series: {},
      trades: trades,
      symbols: [symbol],
      engine_running: false,
      table_empty: "该范围内没有已保存的模拟成交。回看不会补写新成交。",
    };
    view.series[symbol] = {
      symbol: symbol,
      session_date: label + ":" + days,
      is_today: false,
      source: "本地缓存",
      bars: bars,
      quote: null,
    };
    lastKey = "";
    try {
      paintChart(view);
    } catch (err) {
      showChartNote("图表绘制失败：" + (err && err.message ? err.message : "未知错误"));
    }
    if (!bars.length) showChartNote(payload.note || payload.error || "该范围内没有 5 分钟 K 线。");
    if (statusEl) {
      statusEl.textContent = "回看 · " + symbol + " · " + label
        + " · 连续 " + days + " 个交易日 · " + bars.length + " 根K线"
        + " · 已保存成交 " + trades.length + " 笔"
        + (payload.partial ? " · 仅能回补约 60 个自然日" : "");
    }
    if (posEl) posEl.textContent = "";
    if (noteEl) {
      noteEl.textContent = "回看 " + label + "：下表是该范围内已保存的模拟成交，不是单独一天。";
    }
    paintTable(view);
  }

  function setViewMode(mode) {
    viewMode = mode === "replay" ? "replay" : "live";
    var liveBtn = document.getElementById("t-mode-live");
    var replayBtn = document.getElementById("t-mode-replay");
    var sel = document.getElementById("t-replay-date");
    var rangeSel = document.getElementById("t-replay-range");
    var note = document.getElementById("t-replay-note");
    if (liveBtn) liveBtn.classList.toggle("chart-tab-active", viewMode === "live");
    if (replayBtn) replayBtn.classList.toggle("chart-tab-active", viewMode === "replay");
    if (rangeSel) rangeSel.disabled = viewMode !== "replay";
    syncReplayControls();
    if (note) note.hidden = viewMode !== "replay";
    if (viewMode === "live") {
      lastKey = "";
      apply(boot.__last || boot);
      return;
    }
    ensureRange();
  }

  function csrfToken() {
    var el = document.querySelector('input[name="_csrf"]');
    return el ? el.value : "";
  }

  function setReplayDateOptions(dates) {
    var sel = document.getElementById("t-replay-date");
    if (!sel) return false;
    var before = replayDate;
    sel.textContent = "";
    if (!dates.length) {
      var empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "该范围内暂无交易日";
      sel.appendChild(empty);
      replayDate = "";
      return replayDate !== before;
    }
    dates.forEach(function (d) {
      var opt = document.createElement("option");
      opt.value = d;
      opt.textContent = d;
      sel.appendChild(opt);
    });
    sel.value = dates.indexOf(before) >= 0 ? before : dates[0];
    replayDate = sel.value;
    return true;
  }

  function ensureRange() {
    if (viewMode !== "replay") return;
    var symbol = active;
    var range = replayRange || "1m";
    var token = ++replayToken;
    var label = ({
      day: "指定天",
      "1w": "近1周",
      "1m": "近1月",
      "3m": "近3月",
      "6m": "近6月",
      "1y": "近1年",
    })[range] || range;
    showChartNote("正在回补" + label + "的 5 分钟行情…");
    if (!boot.backfill_url || !symbol) {
      showChartNote("无法回补行情。");
      return;
    }
    var body = new URLSearchParams();
    body.set("_csrf", csrfToken());
    body.set("symbol", symbol);
    body.set("range", range);
    fetch(boot.backfill_url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
      },
      body: body.toString(),
      credentials: "same-origin",
    })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (token !== replayToken || viewMode !== "replay" || symbol !== active) return;
        var payload = res.data || {};
        if (!res.ok) {
          showChartNote(payload.error || "范围回补失败。");
          if (range === "day") {
            refreshReplayDates(boot.__last || boot);
            loadReplay();
          }
          return;
        }
        setReplayDateOptions(payload.sessions || []);
        syncReplayControls();
        var extra = payload.note || (payload.partial ? "5 分钟行情最多约 60 个自然日，更早的日期没有 K 线。" : "");
        var replayNote = document.getElementById("t-replay-note");
        if (replayNote) {
          replayNote.hidden = false;
          replayNote.textContent = extra
            ? ("回看不会补写模拟成交。" + extra)
            : (range === "day"
              ? "指定天：选择一个交易日，只看那一天，不会补写新成交。"
              : "该范围的交易日连成一张图，不会补写新成交。");
        }
        if (range === "day") {
          loadReplay();
          return;
        }
        paintContinuous(symbol, label, payload);
      })
      .catch(function () {
        if (token !== replayToken || viewMode !== "replay") return;
        showChartNote("范围回补失败，仅显示已缓存的交易日。");
        if (range === "day") {
          refreshReplayDates(boot.__last || boot);
          loadReplay();
        }
      });
  }

  function syncClearSymbol() {
    var input = document.getElementById("t-clear-symbol");
    if (input && active) input.value = active;
  }

  function startPoll() {
    if (pollTimer) return;
    setBadge("轮询", false);
    const pull = function () {
      fetch(boot.state_url, { headers: { Accept: "application/json" }, cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(apply)
        .catch(function () { setBadge("行情中断", false); });
    };
    pull();
    pollTimer = setInterval(pull, 5000);
  }

  function connect() {
    if (!window.EventSource || !boot.stream_url) {
      startPoll();
      return;
    }
    es = new EventSource(boot.stream_url);
    es.addEventListener("snapshot", function (ev) {
      setBadge("实时", true);
      try {
        apply(JSON.parse(ev.data));
      } catch (err) {
        setBadge("解析失败", false);
      }
    });
    es.onerror = function () {
      if (es) {
        es.close();
        es = null;
      }
      startPoll();
    };
  }

  if (tabs) {
    tabs.addEventListener("click", function (ev) {
      const btn = ev.target.closest("button[data-symbol]");
      if (!btn) return;
      active = btn.getAttribute("data-symbol");
      lastKey = "";
      tabs.querySelectorAll("button[data-symbol]").forEach(function (el) {
        el.classList.toggle("chart-tab-active", el === btn);
      });
      syncClearSymbol();
      if (viewMode === "replay") {
        ensureRange();
      } else {
        apply(boot.__last || boot);
      }
    });
  }

  var liveBtn = document.getElementById("t-mode-live");
  var replayBtn = document.getElementById("t-mode-replay");
  var dateSel = document.getElementById("t-replay-date");
  var rangeSel = document.getElementById("t-replay-range");
  if (liveBtn) liveBtn.addEventListener("click", function () { setViewMode("live"); });
  if (replayBtn) replayBtn.addEventListener("click", function () { setViewMode("replay"); });
  if (rangeSel) rangeSel.addEventListener("change", function () {
    replayRange = rangeSel.value || "1m";
    if (viewMode === "replay") ensureRange();
  });
  if (dateSel) dateSel.addEventListener("change", function () {
    if ((replayRange || "1m") !== "day") return;
    replayDate = dateSel.value;
    if (viewMode === "replay") loadReplay();
  });

  apply(boot);
  connect();
  requestAnimationFrame(function () {
    try {
      resizeChart();
      if (chart) chart.timeScale().fitContent();
    } catch (err) { /* next snapshot retries */ }
  });
  window.addEventListener("resize", function () {
    lastKey = "";
    try { paintChart(boot.__last || boot); } catch (err) { /* status/table stay live */ }
  });
})();
