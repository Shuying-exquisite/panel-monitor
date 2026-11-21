/* 在线状态管理 · 仪表盘逻辑（无外部依赖，图表为纯 SVG） */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
  const icon = (n) => `<svg><use href="#i-${n}"/></svg>`;
  const STATE_TEXT = { online: '在线', offline: '离线', warn: '异常', checking: '检测中', pending: '待检测' };

  let D = null;
  let view = 'overview';
  let trendRange = '7d', detailRange = '1d';
  let keyword = '', keyword2 = '', filter = 'all';
  let showFullUrl = false;   // 网址默认脱敏显示
  let selectedGroup = null;  // 网站管理：当前选中的分组
  let groupDragId = null, groupDropTarget = null, groupDropAfter = false, groupDropValid = false;
  let srvDragId = null, srvDropTarget = null, srvDropAfter = false, srvDropValid = false;
  let settingsFilled = false;
  let chartSeq = 0;

  function safeUrl(u) {
    const raw = String(u || '');
    if (!raw) return '#';
    const full = /^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : 'http://' + raw;
    try {
      const p = new URL(full);
      return (p.protocol === 'http:' || p.protocol === 'https:') ? p.href : '#';
    } catch (e) { return '#'; }
  }

  // 网址脱敏：IP 保留首尾段、域名保留前两位与后缀，端口保留便于辨认
  function maskUrl(u) {
    const raw = String(u || '');
    if (!raw) return '—';
    if (showFullUrl) return raw;
    try {
      const p = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : 'http://' + raw);
      const host = p.hostname;
      let masked;
      if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host)) {
        const seg = host.split('.');
        masked = seg[0] + '.***.***.' + seg[3];
      } else {
        const dot = host.lastIndexOf('.');
        if (dot > 0) {
          const name = host.slice(0, dot);
          masked = name.slice(0, Math.min(2, name.length)) + '***' + host.slice(dot);
        } else {
          masked = host.slice(0, 2) + '***';
        }
      }
      const port = p.port ? ':' + p.port : '';
      const path = (p.pathname && p.pathname !== '/') ? p.pathname : '';
      return p.protocol + '//' + masked + port + path;
    } catch (e) {
      return raw.slice(0, 10) + '***';
    }
  }

  function syncEyeButtons() {
    document.querySelectorAll('[data-eye]').forEach((b) => {
      b.classList.toggle('on', showFullUrl);
      const use = b.querySelector('use');
      if (use) use.setAttribute('href', showFullUrl ? '#i-eye' : '#i-eye-off');
      const label = b.querySelector('span');
      if (label) label.textContent = showFullUrl ? '网址已显示' : '网址已隐藏';
    });
  }

  function toast(text) {
    const el = $('toast');
    el.textContent = text;
    el.classList.add('show');
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('show'), 2200);
  }

  async function api(path, body) {
    const opt = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    };
    const r = await fetch(path, opt);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  async function load(silent) {
    try {
      D = await api(`/api/dashboard?trend=${trendRange}&detail=${detailRange}`);
      if (!dragId && !groupDragId) render();   // 网站或分组拖拽时不重绘，避免打断操作
    } catch (e) {
      if (/HTTP 401/.test(e.message)) { location.replace('/'); return; }  // 会话过期回登录页
      if (!silent) toast('连接本地服务失败：' + e.message);
    }
  }

  // ------------------------------------------------------------ 图表
  function niceMax(v) {
    v = Math.max(1, v || 1);
    if (v <= 16) return Math.ceil(v / 4) * 4;
    const pow = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / pow;
    const m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 4 ? 4 : n <= 5 ? 5 : n <= 8 ? 8 : 10;
    return m * pow;
  }

  function drawChart(el, cfg) {
    if (!el || !D) return;
    const labels = cfg.labels || [];
    const series = cfg.series || [];
    const hasData = series.some((s) => (s.values || []).some((v) => v != null));
    if (!hasData) {
      el.innerHTML = `<div class="chart-empty">${icon('chart')}<span>历史数据采集中 · 每 ${D.interval} 秒记录一次</span></div>`;
      return;
    }
    const W = 620, H = 220, L = 36, T = 14, B = 182;
    const R = cfg.rightAxis ? 560 : 598;
    const n = labels.length || 1;
    const x = (i) => L + (n <= 1 ? (R - L) / 2 : i * (R - L) / (n - 1));
    const leftVals = series.filter((s) => s.axis !== 'right').flatMap((s) => (s.values || []).filter((v) => v != null));
    const yMax = cfg.yMax || niceMax(Math.max.apply(null, leftVals.concat([1])) * 1.05);
    const yLeft = (v) => B - (v / yMax) * (B - T);
    const yRight = (v) => B - (v / 100) * (B - T);
    const uid = 'cg' + (++chartSeq);
    const pal = ['#2f8bff', '#16c784', '#f04b52', '#8b5cf6'];
    const parts = [];

    for (let i = 0; i <= 4; i++) {
      const gy = T + i * (B - T) / 4;
      parts.push(`<line x1="${L}" y1="${gy}" x2="${R}" y2="${gy}" stroke="#eef2f8" stroke-width="1"/>`);
      parts.push(`<text x="${L - 7}" y="${gy + 3}" text-anchor="end" font-size="10" fill="#a8b5c9">${Math.round(yMax * (1 - i / 4))}</text>`);
      if (cfg.rightAxis) parts.push(`<text x="${R + 9}" y="${gy + 3}" font-size="10" fill="#a8b5c9">${Math.round(100 * (1 - i / 4))}%</text>`);
    }

    const defs = [];
    series.forEach((s, si) => {
      const color = s.color || pal[si % pal.length];
      const mapY = s.axis === 'right' ? yRight : yLeft;
      const pts = [];
      (s.values || []).forEach((v, i) => { if (v != null) pts.push([x(i), mapY(v)]); });
      if (s.area && pts.length > 1) {
        const gid = uid + '_' + si;
        defs.push(`<linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${color}" stop-opacity=".38"/><stop offset="100%" stop-color="${color}" stop-opacity=".02"/></linearGradient>`);
        const d = `M ${pts[0][0]} ${B} L ` + pts.map((p) => `${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' L ') + ` L ${pts[pts.length - 1][0]} ${B} Z`;
        parts.push(`<path d="${d}" fill="url(#${gid})"/>`);
      }
      if (pts.length > 1) {
        parts.push(`<polyline points="${pts.map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ')}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>`);
      }
      pts.forEach((p) => parts.push(`<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.8" fill="#fff" stroke="${color}" stroke-width="1.8"/>`));
    });

    const step = Math.max(1, Math.ceil(n / 7));
    labels.forEach((lb, i) => {
      if (i % step === 0 || i === n - 1) {
        parts.push(`<text x="${x(i).toFixed(1)}" y="${B + 16}" text-anchor="middle" font-size="10" fill="#a8b5c9">${esc(lb)}</text>`);
      }
    });
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg"><defs>${defs.join('')}</defs>${parts.join('')}</svg>`;
  }

  function drawDonut(rate) {
    const C = 2 * Math.PI * 62;
    const len = C * Math.max(0, Math.min(100, rate)) / 100;
    $('donut').innerHTML = `<svg viewBox="0 0 160 160" xmlns="http://www.w3.org/2000/svg">
      <circle cx="80" cy="80" r="62" fill="none" stroke="#e6edf7" stroke-width="20"/>
      <circle cx="80" cy="80" r="62" fill="none" stroke="#16c784" stroke-width="20" stroke-linecap="round"
        stroke-dasharray="${len.toFixed(2)} ${(C - len).toFixed(2)}" transform="rotate(-90 80 80)"/>
      <text x="80" y="78" text-anchor="middle" font-size="26" font-weight="700" fill="#1f2b45">${rate}%</text>
      <text x="80" y="98" text-anchor="middle" font-size="11" fill="#8b9ab4">在线率</text>
    </svg>`;
  }

  // ------------------------------------------------------------ 表格
  function mdNow() {
    const d = new Date();
    return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }

  function rowsHTML(list, enableDrag) {
    return list.map((s) => {
      const r = D.results[s.id] || {};
      const st = r.state || 'pending';
      const canVisit = st === 'online' || st === 'warn';
      const href = safeUrl(s.url);
      const latency = r.total_ms != null ? esc(r.total_ms) + 'ms' : '—';
      const checked = r.checked_at ? mdNow() + ' ' + esc(r.checked_at) : '—';
      const handle = enableDrag ? `<span class="drag-handle" title="按住拖动排序">${icon('grip')}</span>` : '';
      const displayUrl = maskUrl(s.url);
      return `<tr data-id="${esc(s.id)}"${enableDrag ? ' data-drag="1"' : ''}>
        <td><div class="t-name">${handle}<span class="t-ico">${icon('globe')}</span>
          ${canVisit ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer" title="点击访问">${esc(s.name)}</a>` : `<span title="服务未在线">${esc(s.name)}</span>`}
        </div></td>
        <td><span class="t-url${showFullUrl ? '' : ' masked'}" title="${esc(displayUrl)}">${esc(displayUrl)}</span></td>
        <td><span class="t-state ${st}"><i></i>${STATE_TEXT[st] || st}</span></td>
        <td>${latency}</td>
        <td>${checked}</td>
        <td><div class="t-ops">
          <button class="t-btn" data-act="detail">详情</button>
          <div class="menu-pop">
            <button class="t-dots" data-act="menu" title="更多">${icon('dots')}</button>
            <div class="menu-list hidden">
              <button data-act="visit" ${canVisit ? '' : 'disabled style="opacity:.45;cursor:not-allowed"'}>${icon('external')}访问面板</button>
              <button data-act="edit">${icon('edit')}编辑</button>
              <button data-act="delete" class="danger">${icon('trash')}删除</button>
            </div>
          </div>
        </div></td>
      </tr>`;
    }).join('');
  }

  function tableHTML(list, enableDrag) {
    if (!list.length) return `<div class="empty-row">没有匹配的网站</div>`;
    return `<table><thead><tr>
      <th>网站名称</th><th>网址</th><th>状态</th><th>响应时间</th><th>最后检测</th><th>操作</th>
    </tr></thead><tbody>${rowsHTML(list, enableDrag)}</tbody></table>`;
  }

  // ------------------------------------------------------------ 分组卡片（网站管理）
  function groupList() {
    const map = new Map();
    D.servers.forEach((s) => {
      const g = s.group || '默认';
      if (!map.has(g)) map.set(g, []);
      map.get(g).push(s);
    });
    return Array.from(map, ([name, items]) => ({ name, items }));
  }

  function renderGroupCards() {
    const host = $('group-cards');
    if (!host) return;
    const groups = groupList();
    // 选中的分组被删空时，自动回落到第一个分组
    if (!groups.some((g) => g.name === selectedGroup)) {
      selectedGroup = groups.length ? groups[0].name : null;
    }
    $('group-total').textContent = groups.length + ' 组';
    $('sites-title').textContent = selectedGroup ? selectedGroup + ' · 网站详情' : '网站详情';

    host.innerHTML = groups.map((g) => {
      let online = 0;
      g.items.forEach((s) => {
        if ((D.results[s.id] || {}).state === 'online') online++;
      });
      const bad = g.items.length - online;
      const rate = g.items.length ? Math.round(100 * online / g.items.length) : 0;
      return `<button class="group-card${g.name === selectedGroup ? ' active' : ''}" data-group-card="${esc(g.name)}">
        <div class="gc-top"><span class="drag-handle gc-grip" title="按住拖动分组排序">${icon('grip')}</span><span class="gc-folder">${icon('folder')}</span><span class="gc-name">${esc(g.name)}</span><span class="gc-count">${g.items.length}</span></div>
        <div class="gc-summary"><span class="gs-online">● 在线 ${online}</span><span class="gs-bad">● 非在线 ${bad}</span></div>
        <div class="gc-bar"><i style="width:${rate}%"></i></div>
      </button>`;
    }).join('') || '<div class="empty-row">还没有分组</div>';
  }

  function renderTables() {
    const all = D.servers.map((s) => Object.assign({}, s, { r: D.results[s.id] || {} }));
    const configs = [
      { ctx: 'overview', host: $('table-overview'), count: $('count-overview'), pager: $('pager-overview'), kw: keyword, useFilter: true },
      { ctx: 'sites', host: $('table-sites'), count: $('count-sites'), pager: $('pager-sites'), kw: keyword2, useFilter: false, onlyGroup: true },
    ];
    configs.forEach((c) => {
      if (!c.host) return;
      const list = all.filter((s) => {
        if (c.onlyGroup && selectedGroup && (s.group || '默认') !== selectedGroup) return false;
        if (c.kw && ![s.name, s.url, s.note, s.group].join(' ').toLowerCase().includes(c.kw.toLowerCase())) return false;
        if (c.useFilter && filter !== 'all') {
          const st = s.r.state || 'pending';
          if (filter === 'offline' && st !== 'offline' && st !== 'warn') return false;
          if (filter === 'online' && st !== 'online') return false;
          if (filter === 'warn' && st !== 'warn') return false;
        }
        return true;
      });
      // 不分页：一次显示全部记录
      c.host.innerHTML = tableHTML(list, c.ctx === 'sites');
      c.count.textContent = c.onlyGroup
        ? `${selectedGroup || '全部分组'} · 共 ${list.length} 个网站 · 全部显示`
        : `共 ${list.length} 条记录 · 全部显示`;
      if (c.pager) c.pager.innerHTML = '';
    });
  }

  // ------------------------------------------------------------ 主渲染
  function render() {
    if (!D) return;
    const s = D.stats;
    $('s-total').textContent = s.total;
    $('s-online').textContent = s.online;
    $('s-offline').textContent = s.offline;
    $('s-rate').textContent = s.rate + '%';
    $('d-online').textContent = s.online;
    $('d-offline').textContent = s.offline;
    drawDonut(s.rate);

    $('k-avg').textContent = D.kpi.avg_ms != null ? D.kpi.avg_ms + 'ms' : '—';
    $('k-long').textContent = D.kpi.longest_online;
    $('k-last').textContent = D.kpi.last_offline;

    const sys = document.querySelector('.sys-status');
    sys.classList.toggle('bad', s.offline > 0);
    $('sys-text').textContent = s.offline > 0 ? `有 ${s.offline} 台服务离线 / 异常` : '系统运行正常';
    const alerts = (D.events || []).filter((e) => e.level === 'warn').length;
    $('bell-badge').style.display = alerts ? 'grid' : 'none';
    $('bell-badge').textContent = alerts > 99 ? '99+' : alerts;
    $('q-check').style.opacity = D.checking ? '.6' : '1';
    $('btn-logout').style.display = D.auth_enabled ? 'inline-flex' : 'none';

    // 趋势图（总览 + 监控页）
    const t = D.trend;
    const trendCfg = {
      labels: t.labels,
      yMax: niceMax(Math.max.apply(null, (t.total || []).filter((v) => v != null).concat([1])) * 1.08),
      series: [
        { values: t.total, color: '#2f8bff' },
        { values: t.online, color: '#16c784' },
        { values: t.offline, color: '#f04b52' },
      ],
    };
    drawChart($('chart-trend'), trendCfg);
    drawChart($('chart-trend-big'), trendCfg);
    $('trend-note').textContent = `采样点 ${t.samples} 个 · 数据来源：每 ${D.interval} 秒一次真实检测`;
    $('monitor-trend-note').textContent = $('trend-note').textContent;
    $('monitor-range-label').textContent = $('trend-range').selectedOptions[0].text;

    // 双轴监控详情
    const d = D.timeline;
    const latVals = (d.latency || []).filter((v) => v != null);
    const detailCfg = {
      labels: d.labels, rightAxis: true,
      yMax: niceMax(Math.max.apply(null, latVals.concat([60])) * 1.35),
      series: [
        { values: d.latency, color: '#2f8bff', area: true },
        { values: d.availability, color: '#16c784', axis: 'right' },
      ],
    };
    drawChart($('chart-detail'), detailCfg);
    drawChart($('chart-detail-big'), detailCfg);
    const note = `采样点 ${d.samples} 个 · 左轴响应时间(ms) / 右轴可用性(%)`;
    $('detail-note').textContent = note;
    $('monitor-detail-note').textContent = note;

    renderGroupCards();
    renderTables();
    renderLogs();
    renderAbout();
    renderSyncStatus();
    renderFan();
  }

  function renderLogs() {
    const host = $('log-host');
    if (!host) return;
    const list = D.events || [];
    host.innerHTML = list.length ? list.map((e) => `<div class="log-row">
      <span class="log-time">${esc(e.time)}</span>
      <span class="log-tag ${esc(e.level)}">${e.level === 'warn' ? '告警' : e.level === 'ok' ? '恢复' : '信息'}</span>
      <span class="log-text">${esc(e.text)}</span>
    </div>`).join('') : '<div class="empty-row">暂无日志记录</div>';
  }

  function renderAbout() {
    $('about-version').textContent = D.version;
    $('about-port').textContent = '127.0.0.1:' + D.port;
    $('about-config').textContent = D.config_path;
    $('about-uptime').textContent = D.uptime;
    $('settings-meta').innerHTML = `配置文件：<b>${esc(D.config_path)}</b><br>历史数据：<b>${esc(D.store_path)}</b><br>当前采样点：<b>${D.kpi.samples}</b> 个 · 服务已运行 <b>${esc(D.uptime)}</b>`;
  }

  function fillSettings() {
    $('set-interval').value = D.interval;
    $('set-alert').checked = !!D.alert_sound;
    const sc = D.sync || {};
    if ($('set-sync-url')) $('set-sync-url').value = sc.url || '';
    if ($('set-sync-user')) $('set-sync-user').value = sc.user || '';
    if ($('set-sync-pass')) $('set-sync-pass').value = '';   // 不回显明文密码
    if ($('set-sync-auto')) $('set-sync-auto').checked = !!sc.auto;
    if ($('set-sync-interval')) $('set-sync-interval').value = sc.interval || 30;
    fillFanForm();
    settingsFilled = true;
  }

  // 从 D.fan 填充风扇表单（系统设置卡片）；只填表单控件，不碰显示层
  function fillFanForm() {
    const f = (D && D.fan) || {};
    if ($('fan-enabled')) $('fan-enabled').checked = !!f.enabled;
    if ($('fan-gpio')) $('fan-gpio').value = (f.gpio != null) ? f.gpio : 18;
    if ($('fan-inverted')) $('fan-inverted').checked = !!f.inverted;
  }

  function renderSyncStatus() {
    const ls = (D && D.last_sync) || { time: 0, ok: null, msg: '尚未同步' };
    const last = $('sync-last');
    if (last) {
      if (ls.time) {
        const d = new Date(ls.time * 1000);
        const t = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
        last.textContent = (ls.ok ? '上次成功 ' : '上次失败 ') + t;
        last.style.color = ls.ok ? '' : 'var(--red)';
      } else {
        last.textContent = '尚未同步';
        last.style.color = '';
      }
    }
    const box = $('sync-status');
    if (box && ls.msg) {
      box.textContent = ls.msg;
      box.className = 'sync-status ' + (ls.ok === false ? 'bad' : ls.ok === true ? 'ok' : '');
    }
  }

  // ------------------------------------------------------------ 风扇
  function renderFan() {
    const f = (D && D.fan) || {};
    const temp = (D && D.temp != null) ? D.temp : null;
    const avail = !!f.available;
    const enabledLocal = $('fan-enabled') ? $('fan-enabled').checked : false;  // 以本地勾选为准，避免刷新打断输入

    // 系统设置卡片：可用性提示
    const availEl = $('fan-available');
    if (availEl) {
      availEl.textContent = avail ? 'GPIO 可用' : '当前环境不支持 GPIO（仅树莓派端可用）';
      availEl.style.color = avail ? 'var(--green)' : 'var(--orange)';
    }
    // 系统设置卡片：实时面板
    const live = $('fan-live');
    if (live) live.style.display = (enabledLocal && avail) ? '' : 'none';
    // 总览快速卡：仅在可用且已启用时显示
    const quick = $('fan-quick');
    if (quick) quick.style.display = (avail && enabledLocal) ? '' : 'none';

    // 状态点 / 文本（设置卡 + 总览卡）
    const stateOn = avail && enabledLocal && f.state === 1;
    const stateText = !avail ? '不可用' : (enabledLocal ? (f.state === 1 ? '已打开' : '已关闭') : '已禁用');
    ['fan-state-text', 'fan-quick-state'].forEach((id) => { const el = $(id); if (el) el.textContent = stateText; });
    ['fan-dot', 'fan-quick-dot'].forEach((id) => { const el = $(id); if (el) el.classList.toggle('on', stateOn); });
    // 温度
    ['fan-temp', 'fan-quick-temp'].forEach((id) => { const el = $(id); if (el) el.textContent = (temp != null) ? temp : '—'; });
  }

  // ------------------------------------------------------------ 服务器运行状态
  function sysFmtBytes(b) {
    if (b == null || b <= 0) return '—';
    const g = 1024 * 1024 * 1024, m = 1024 * 1024;
    if (b >= g) return (b / g).toFixed(2) + ' GB';
    if (b >= m) return (b / m).toFixed(2) + ' MB';
    return (b / 1024).toFixed(2) + ' KB';
  }

  const num1 = (v) => (v == null || isNaN(v) ? '—' : Number(v).toFixed(1));
  function setRing(id, pct) {
    const el = $(id);
    if (!el) return;
    const p = (pct == null) ? 0 : Math.max(0, Math.min(100, pct));
    el.style.setProperty('--pct', p);
    el.style.setProperty('--ring-color', p >= 85 ? 'var(--red)' : p >= 65 ? 'var(--orange)' : 'var(--blue)');
  }
  function typeLabel(t) { return t === 'baota' ? '宝塔' : t === 'agent' ? '采集器' : '1Panel'; }

  let statusServers = [];   // 独立运行状态服务器（与网站无关）
  let statusCache = {};     // id -> 最近一次状态
  let selectedSrvId = null; // 当前选中的服务器
  let selectedSrvGroup = null; // 当前选中的分组

  // 按分组名顺序提取分组（保持服务器出现顺序）
  function srvGroups() {
    const seen = [];
    statusServers.forEach((s) => {
      const g = s.group || '默认分组';
      if (!seen.includes(g)) seen.push(g);
    });
    return seen;
  }
  function inCurrentGroup() {
    return statusServers.filter((s) => (s.group || '默认分组') === selectedSrvGroup);
  }

  // 渲染分组栏（只在服务器分组变化时重建）
  function renderGroupList() {
    const box = $('sys-group-list');
    if (!box) return;
    const groups = srvGroups();
    if (!groups.length) {
      box.innerHTML = '<div class="sys-side-empty" style="padding:30px 8px">暂无分组</div>';
      if ($('sys-group-total')) $('sys-group-total').textContent = '0 组';
      return;
    }
    if (!groups.includes(selectedSrvGroup)) selectedSrvGroup = groups[0];
    box.innerHTML = groups.map((g) => {
      const n = statusServers.filter((s) => (s.group || '默认分组') === g).length;
      return `<button class="sys-group-item${g === selectedSrvGroup ? ' active' : ''}" data-group="${esc(g)}">
        <span class="sg-top"><span class="sg-folder">${icon('folder')}</span><span class="sg-name">${esc(g)}</span><span class="sg-count">${n}</span></span>
        <span class="sg-status" data-role="gstatus"><span class="sg-on">在线 <b>0</b></span><span class="sg-off">离线 <b>0</b></span></span>
      </button>`;
    }).join('');
    if ($('sys-group-total')) $('sys-group-total').textContent = groups.length + ' 组';
    if ($('sys-list-title')) $('sys-list-title').textContent = selectedSrvGroup;
    // 同步弹窗分组候选
    const dl = $('sf-group-list');
    if (dl) dl.innerHTML = groups.map((g) => `<option value="${esc(g)}"></option>`).join('');
    updateGroupStatus();
  }

  // 刷新时只更新各分组的在线/离线数字（不重建分组栏 → 无闪烁）
  function updateGroupStatus() {
    document.querySelectorAll('.sys-group-item').forEach((card) => {
      const g = card.getAttribute('data-group');
      let online = 0, offline = 0;
      statusServers.forEach((s) => {
        if ((s.group || '默认分组') !== g) return;
        const st = statusCache[s.id];
        if (st && st.ok !== false) online++; else offline++;
      });
      const onEl = card.querySelector('.sg-on b');
      const offEl = card.querySelector('.sg-off b');
      if (onEl) onEl.textContent = online;
      if (offEl) offEl.textContent = offline;
    });
  }

  // 中栏标题的数量 = 当前分组的服务器台数
  function updateListCount() {
    const n = inCurrentGroup().length;
    if ($('sys-count')) $('sys-count').textContent = n ? `共 ${n} 台` : '';
  }

  // 切换分组：重建中间服务器列表，选中该组第一台
  function selectGroup(name) {
    selectedSrvGroup = name;
    document.querySelectorAll('.sys-group-item').forEach((el) => el.classList.toggle('active', el.getAttribute('data-group') === name));
    if ($('sys-list-title')) $('sys-list-title').textContent = name;
    updateListCount();
    renderSideList();
    const groupServers = inCurrentGroup();
    selectedSrvId = groupServers.length ? groupServers[0].id : null;
    if (selectedSrvId) selectServer(selectedSrvId);
    else {
      if ($('sys-empty')) $('sys-empty').classList.remove('hidden');
      if ($('sys-detail-wrap')) $('sys-detail-wrap').classList.add('hidden');
    }
  }

  // 只在服务器增删改/换组时重建中间小卡列表（避免闪烁）
  function renderSideList() {
    const box = $('sys-side-list');
    if (!box) return;
    const list = inCurrentGroup();
    if (!list.length) {
      box.innerHTML = '<div class="sys-side-empty">该分组暂无服务器，点上方「添加」</div>';
      return;
    }
    box.innerHTML = list.map((s) => `
      <div class="side-card" data-id="${esc(s.id)}" draggable="false">
        <div class="sc-top"><span class="drag-handle sc-grip" title="按住拖动排序">${icon('grip')}</span><span class="scard-dot off" data-role="dot"></span><span class="sc-name">${esc(s.name)}</span></div>
        <div class="sc-stats"><span>CPU <b data-role="cpu">—</b></span><span>内存 <b data-role="mem">—</b></span></div>
      </div>`).join('');
  }

  // 只更新左侧小卡内的文本/状态（不重建 DOM）
  function updateSideList() {
    statusServers.forEach((s) => {
      const card = document.querySelector(`.side-card[data-id="${CSS.escape(s.id)}"]`);
      if (!card) return;
      const st = statusCache[s.id];
      const online = !!(st && st.ok !== false);
      const dot = card.querySelector('[data-role="dot"]');
      if (dot) dot.className = 'scard-dot' + (online ? '' : ' off');
      const cpuEl = card.querySelector('[data-role="cpu"]');
      const memEl = card.querySelector('[data-role="mem"]');
      if (cpuEl) cpuEl.textContent = online && st.cpu && st.cpu.percent != null ? num1(st.cpu.percent) + '%' : '—';
      if (memEl) memEl.textContent = online && st.mem && st.mem.percent != null ? num1(st.mem.percent) + '%' : '—';
    });
  }

  // 切换选中服务器（只更新标题，不重建右侧 DOM）
  function selectServer(id) {
    selectedSrvId = id;
    document.querySelectorAll('.side-card').forEach((el) => el.classList.toggle('active', el.getAttribute('data-id') === id));
    const srv = statusServers.find((s) => s.id === id);
    if (!srv) return;
    if ($('sys-empty')) $('sys-empty').classList.add('hidden');
    if ($('sys-detail-wrap')) $('sys-detail-wrap').classList.remove('hidden');
    if ($('sd-name')) $('sd-name').textContent = srv.name;
    if ($('sd-type')) $('sd-type').textContent = typeLabel(srv.type);
    updateDetail(statusCache[id]);
  }

  // 只更新右侧详情的文本和圆环（不重建 DOM → 无闪烁）
  function updateDetail(st) {
    if (!selectedSrvId) return;
    const online = !!(st && st.ok !== false);
    const dot = $('sd-dot');
    if (dot) dot.className = 'scard-dot' + (online ? '' : ' off');
    const cpu = online && st.cpu ? st.cpu : {}, mem = online && st.mem ? st.mem : {}, load = online && st.load ? st.load : {}, disk = online && st.disk ? st.disk : {};
    let loadPct = load.percent;
    if (loadPct == null && load.load1 != null && (cpu.cores || 0) > 0) loadPct = Math.min(100, load.load1 / cpu.cores * 100);
    setRing('sys-ring-cpu', online ? cpu.percent : null);
    setRing('sys-ring-mem', online ? mem.percent : null);
    setRing('sys-ring-load', online ? loadPct : null);
    setRing('sys-ring-disk', online ? disk.percent : null);
    const txt = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    txt('sys-cpu-val', online ? num1(cpu.percent) + (cpu.percent == null ? '' : ' %') : '—');
    txt('sys-mem-val', online ? num1(mem.percent) + (mem.percent == null ? '' : ' %') : '—');
    txt('sys-load-val', online ? num1(loadPct) + (loadPct == null ? '' : ' %') : '—');
    txt('sys-disk-val', online ? num1(disk.percent) + (disk.percent == null ? '' : ' %') : '—');
    txt('sys-cpu-sub', online ? (cpu.cores ? (cpu.percent == null ? '—' : (cpu.percent / 100 * cpu.cores).toFixed(2)) + ' / ' + cpu.cores + ' 核' : '核心数 —') : '—');
    txt('sys-mem-sub', online ? (mem.total ? `${sysFmtBytes(mem.used)} / ${sysFmtBytes(mem.total)}` : '—') : '—');
    txt('sys-load-sub', online ? (load.load1 == null ? '—' : (load.load1 < (cpu.cores || 99) ? '运行流畅' : '负载偏高')) : '—');
    txt('sys-disk-sub', online ? (disk.total ? `${sysFmtBytes(disk.used)} / ${sysFmtBytes(disk.total)}` : '—') : '—');
    txt('sys-temp', online ? (st.temp == null ? '—（采集器不提供）' : st.temp + ' ℃') : '—');
    txt('sys-uptime', online ? (st.uptime || '—') : (st && st.error ? '读取失败' : '—'));
    txt('sys-cores', online ? ((cpu.cores || '—') + (cpu.cores ? ' 核' : '')) : '—');
    const l15 = online && load.load5 != null && load.load15 != null
      ? `${num1(load.load1)} / ${num1(load.load5)} / ${num1(load.load15)}`
      : (online && load.load1 != null ? num1(load.load1) : '—');
    txt('sys-load-full', l15);
    txt('sys-updated', online
      ? '更新于 ' + (st.ts ? new Date(st.ts * 1000).toLocaleTimeString('zh-CN') : '—')
      : (st && st.error ? '读取失败：' + st.error : '—'));
  }

  // 拉取列表 + 并行状态，更新缓存与界面（不重建右侧 DOM）
  async function loadAllSystem(manual) {
    if (srvDragId) return;   // 拖拽排序中跳过自动刷新，避免打断操作
    const btn = $('sys-refresh');
    if (manual && btn) btn.classList.add('spin');
    try {
      const j = await api('/api/panel/list');
      const next = (j && j.servers) || [];
      // 列表/名称/分组变化时重建（比较 id+name+type+group）
      // 注意：不可排序——顺序本身是签名的一部分，否则拖拽换序后检测不到变化
      const sigOf = (arr) => arr.map((s) => `${s.id}|${s.name}|${s.type}|${s.group || ''}`).join(',');
      if (sigOf(next) !== sigOf(statusServers)) {
        statusServers = next;
        renderGroupList();
        renderSideList();
        // 选中项失效则默认当前组第一台
        if (!inCurrentGroup().some((s) => s.id === selectedSrvId)) {
          const groupServers = inCurrentGroup();
          selectedSrvId = groupServers.length ? groupServers[0].id : null;
        }
        if (!selectedSrvId) {
          if ($('sys-empty')) $('sys-empty').classList.remove('hidden');
          if ($('sys-detail-wrap')) $('sys-detail-wrap').classList.add('hidden');
        }
      } else {
        statusServers = next;
      }
    } catch (e) {
      statusServers = [];
    }
    updateListCount();
    // 并行拉所有状态存入缓存
    await Promise.all(statusServers.map(async (srv) => {
      try {
        const r = await api('/api/panel/status?id=' + encodeURIComponent(srv.id));
        statusCache[srv.id] = (r && r.ok && r.status) ? r.status : { ok: false, error: (r && r.error) || '读取失败' };
      } catch (e) {
        if (/HTTP 401/.test(e.message)) { location.replace('/'); return; }
        statusCache[srv.id] = { ok: false, error: e.message };
      }
    }));
    updateSideList();
    updateGroupStatus();   // 各分组在线/离线数字
    if (selectedSrvId) selectServer(selectedSrvId);   // 仅更新文本/圆环，无重建闪烁
    if (manual && btn) setTimeout(() => btn.classList.remove('spin'), 400);
  }

  // ------------------------------------------------------------ 视图
  function switchView(v) {
    view = v;
    document.querySelectorAll('.view').forEach((el) => el.classList.add('hidden'));
    $('view-' + v).classList.remove('hidden');
    document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === v));
    if (v === 'settings' && D) fillSettings();
    if (v === 'system') { loadAllSystem(true); }
  }

  // ------------------------------------------------------------ 弹窗
  function openEdit(srv) {
    $('edit-title').textContent = srv ? '编辑网站' : '添加网站';
    $('f-id').value = srv ? srv.id : '';
    $('f-name').value = srv ? srv.name : '';
    $('f-url').value = srv ? srv.url : '';
    $('f-group').value = srv ? (srv.group || '') : '';
    $('f-mode').value = srv ? (srv.mode || 'http') : 'http';
    $('f-timeout').value = srv ? (srv.timeout || 3) : 3;
    $('f-note').value = srv ? (srv.note || '') : '';
    $('edit-modal').classList.remove('hidden');
    setTimeout(() => $('f-name').focus(), 50);
  }
  function closeEdit() { $('edit-modal').classList.add('hidden'); }

  async function saveEdit() {
    const url = $('f-url').value.trim();
    if (!$('f-name').value.trim()) { toast('请填写网站名称'); return; }
    if (!url) { toast('请填写网址'); return; }
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(url) && !/^https?:\/\//i.test(url)) { toast('只支持 http:// 或 https://'); return; }
    try {
      const res = await api('/api/server/save', {
        id: $('f-id').value, name: $('f-name').value.trim(), url,
        group: $('f-group').value.trim(), mode: $('f-mode').value,
        timeout: $('f-timeout').value, note: $('f-note').value.trim(),
      });
      // 保存后自动切到它所属的分组，方便立刻看到结果
      if (res && res.ok && res.server) selectedGroup = res.server.group || '默认';
      closeEdit(); toast('已保存，正在检测…'); await load();
    } catch (e) { toast('保存失败：' + e.message); }
  }

  function openDetail(id) {
    const srv = D.servers.find((s) => s.id === id);
    if (!srv) return;
    const r = D.results[id] || {};
    const st = r.state || 'pending';
    const canVisit = st === 'online' || st === 'warn';
    $('dm-title').textContent = srv.name;
    $('dm-body').innerHTML = `<div class="dm-grid">
      <span>网址</span><b>${esc(srv.url || '—')}</b>
      <span>分组</span><b>${esc(srv.group || '默认')}</b>
      <span>状态</span><b class="t-state ${st}" style="display:inline-flex"><i></i>${STATE_TEXT[st] || st}</b>
      <span>检测方式</span><b>${srv.mode === 'tcp' ? '仅 TCP 端口' : 'HTTP 请求'}</b>
      <span>响应时间</span><b>${r.total_ms != null ? esc(r.total_ms) + ' ms' : '—'}</b>
      <span>TCP 握手</span><b>${r.tcp_ms != null ? esc(r.tcp_ms) + ' ms' : '—'}</b>
      <span>HTTP 状态</span><b>${r.status != null ? esc(r.status) : '—'}</b>
      <span>页面标题</span><b>${esc(r.title || '—')}</b>
      <span>结果说明</span><b>${esc(r.message || '—')}</b>
      <span>最后检测</span><b>${esc(r.checked_at || D.last_check || '—')}</b>
      <span>备注</span><b>${esc(srv.note || '—')}</b>
    </div>`;
    const a = $('dm-visit');
    a.href = canVisit ? safeUrl(srv.url) : '#';
    a.style.opacity = canVisit ? '1' : '.45';
    a.style.pointerEvents = canVisit ? 'auto' : 'none';
    a.textContent = canVisit ? '访问面板' : '离线，不可访问';
    $('detail-modal').classList.remove('hidden');
  }

  // ------------------------------------------------------------ 拖拽排序
  let dragId = null, dropTargetId = null, dropAfter = false, dropValid = false;

  function clearDragStyles(host) {
    host.querySelectorAll('tr[data-id]').forEach((tr) => {
      tr.classList.remove('dragging', 'drop-before', 'drop-after');
    });
  }

  function resetDrag(host) {
    clearDragStyles(host);
    dragId = null; dropTargetId = null; dropAfter = false; dropValid = false;
    host.querySelectorAll('tr[data-id]').forEach((x) => { x.draggable = false; });
  }

  function initDrag() {
    const host = $('table-sites');
    if (!host) return;
    // 只有从行首手柄按下才允许拖动，避免影响链接和按钮
    host.addEventListener('mousedown', (e) => {
      const tr = e.target.closest('tr[data-id]');
      if (tr) tr.draggable = !!e.target.closest('.drag-handle');
    });
    host.addEventListener('dragstart', (e) => {
      const tr = e.target.closest('tr[data-id]');
      if (!tr || !tr.draggable) { e.preventDefault(); return; }
      dragId = tr.dataset.id;
      tr.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', dragId); } catch (_) { /* 某些浏览器限制 */ }
    });
    host.addEventListener('dragover', (e) => {
      if (!dragId) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      host.querySelectorAll('tr.drop-before, tr.drop-after').forEach((x) => x.classList.remove('drop-before', 'drop-after'));
      dropTargetId = null; dropAfter = false; dropValid = false;
      const tr = e.target.closest('tr[data-id]');
      if (!tr || tr.dataset.id === dragId) return;
      dropValid = true;
      dropTargetId = tr.dataset.id;
      const rect = tr.getBoundingClientRect();
      dropAfter = e.clientY > rect.top + rect.height / 2;
      tr.classList.add(dropAfter ? 'drop-after' : 'drop-before');
    });
    host.addEventListener('drop', async (e) => {
      e.preventDefault();
      if (!dragId) return;
      const movingId = dragId;
      const valid = dropValid, target = dropTargetId, after = dropAfter;
      const visible = Array.from(host.querySelectorAll('tr[data-id]')).map((x) => x.dataset.id);
      resetDrag(host);
      if (!valid) return;                       // 落在自身或表格外，视为取消

      const from = visible.indexOf(movingId);
      if (from >= 0) visible.splice(from, 1);
      let pos = visible.indexOf(target);
      if (pos < 0) pos = visible.length;
      else if (after) pos += 1;
      visible.splice(pos, 0, movingId);

      // 把当前页的新顺序回填到全局列表对应的槽位，跨页时其它行保持不动
      const pageSet = new Set(visible);
      const globalIds = D.servers.map((s) => s.id);
      let k = 0;
      const newOrder = globalIds.map((id) => pageSet.has(id) ? visible[k++] : id);
      if (newOrder.join('|') === globalIds.join('|')) return;
      try {
        const res = await api('/api/servers/reorder', { ids: newOrder });
        if (!res.ok) { toast('排序失败，列表已变化，请刷新重试'); return; }
        toast('顺序已保存');
        await load(true);
      } catch (err) { toast('保存失败：' + err.message); }
    });
    host.addEventListener('dragleave', (e) => {
      if (!host.contains(e.relatedTarget)) {
        host.querySelectorAll('tr.drop-before, tr.drop-after').forEach((x) => x.classList.remove('drop-before', 'drop-after'));
      }
    });
    host.addEventListener('dragend', () => resetDrag(host));
  }

  // ------------------------------------------------------------ 分组拖拽排序
  function resetGroupDrag(host) {
    host.querySelectorAll('.group-card').forEach((c) => {
      c.classList.remove('dragging', 'drop-before', 'drop-after');
      c.draggable = false;
    });
    groupDragId = null; groupDropTarget = null; groupDropAfter = false; groupDropValid = false;
  }

  // 按新的分组顺序重排 servers 数组（组内保持原顺序），供 /api/servers/reorder 落盘
  function buildGroupOrderIds(names) {
    const byGroup = new Map();
    D.servers.forEach((s) => {
      const g = s.group || '默认';
      if (!byGroup.has(g)) byGroup.set(g, []);
      byGroup.get(g).push(s.id);
    });
    const ids = [];
    names.forEach((g) => (byGroup.get(g) || []).forEach((id) => ids.push(id)));
    byGroup.forEach((v, k) => { if (!names.includes(k)) v.forEach((id) => ids.push(id)); });
    return ids;
  }

  function initGroupDrag() {
    const host = $('group-cards');
    if (!host) return;
    // 只有从手柄按下才允许拖动，卡片本身仍是「点击切换分组」
    host.addEventListener('mousedown', (e) => {
      const card = e.target.closest('.group-card');
      if (card) card.draggable = !!e.target.closest('.drag-handle');
    });
    host.addEventListener('dragstart', (e) => {
      const card = e.target.closest('.group-card');
      if (!card || !card.draggable) { e.preventDefault(); return; }
      groupDragId = card.dataset.groupCard;
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', groupDragId); } catch (_) { /* 浏览器限制 */ }
    });
    host.addEventListener('dragover', (e) => {
      if (!groupDragId) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      host.querySelectorAll('.group-card').forEach((c) => c.classList.remove('drop-before', 'drop-after'));
      groupDropTarget = null; groupDropAfter = false; groupDropValid = false;
      const card = e.target.closest('.group-card');
      if (!card || card.dataset.groupCard === groupDragId) return;
      groupDropValid = true;
      groupDropTarget = card.dataset.groupCard;
      const rect = card.getBoundingClientRect();
      groupDropAfter = e.clientY > rect.top + rect.height / 2;
      card.classList.add(groupDropAfter ? 'drop-after' : 'drop-before');
    });
    host.addEventListener('drop', async (e) => {
      e.preventDefault();
      if (!groupDragId) return;
      const moving = groupDragId;
      const valid = groupDropValid, target = groupDropTarget, after = groupDropAfter;
      const names = Array.from(host.querySelectorAll('.group-card')).map((c) => c.dataset.groupCard);
      resetGroupDrag(host);
      if (!valid) return;   // 落在自身或空白处，视为取消

      const from = names.indexOf(moving);
      if (from >= 0) names.splice(from, 1);
      let pos = names.indexOf(target);
      if (pos < 0) pos = names.length;
      else if (after) pos += 1;
      names.splice(pos, 0, moving);

      // 分组顺序由 servers 数组里各组首次出现的先后决定；复用 /api/servers/reorder 落盘
      const ids = buildGroupOrderIds(names);
      if (ids.join('|') === D.servers.map((s) => s.id).join('|')) return;
      try {
        const res = await api('/api/servers/reorder', { ids });
        if (!res.ok) { toast('分组排序失败，请刷新重试'); return; }
        toast('分组顺序已保存');
        await load(true);
      } catch (err) { toast('保存失败：' + err.message); }
    });
    host.addEventListener('dragend', () => resetGroupDrag(host));
  }

  // 服务器小卡拖拽排序（仅从手柄按下才允许拖动，卡片本身仍是点击选中）
  function resetSrvDrag(host) {
    if (host) host.querySelectorAll('.side-card').forEach((c) => c.classList.remove('dragging', 'drop-before', 'drop-after'));
    srvDragId = null; srvDropTarget = null; srvDropAfter = false; srvDropValid = false;
  }
  function initStatusDrag() {
    const host = $('sys-side-list');
    if (!host) return;
    host.addEventListener('mousedown', (e) => {
      const card = e.target.closest('.side-card');
      if (card) card.draggable = !!e.target.closest('.drag-handle');
    });
    host.addEventListener('dragstart', (e) => {
      const card = e.target.closest('.side-card');
      if (!card || !card.draggable) { e.preventDefault(); return; }
      srvDragId = card.dataset.id;
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', srvDragId); } catch (_) { /* 浏览器限制 */ }
    });
    host.addEventListener('dragover', (e) => {
      if (!srvDragId) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      host.querySelectorAll('.side-card').forEach((c) => c.classList.remove('drop-before', 'drop-after'));
      srvDropTarget = null; srvDropAfter = false; srvDropValid = false;
      const card = e.target.closest('.side-card');
      if (!card || card.dataset.id === srvDragId) return;
      srvDropValid = true;
      srvDropTarget = card.dataset.id;
      const rect = card.getBoundingClientRect();
      srvDropAfter = e.clientY > rect.top + rect.height / 2;
      card.classList.add(srvDropAfter ? 'drop-after' : 'drop-before');
    });
    host.addEventListener('drop', async (e) => {
      e.preventDefault();
      if (!srvDragId) return;
      const moving = srvDragId;
      const valid = srvDropValid, target = srvDropTarget, after = srvDropAfter;
      const names = Array.from(host.querySelectorAll('.side-card')).map((c) => c.dataset.id);
      resetSrvDrag(host);
      if (!valid) return;
      const from = names.indexOf(moving);
      if (from >= 0) names.splice(from, 1);
      let pos = names.indexOf(target);
      if (pos < 0) pos = names.length;
      else if (after) pos += 1;
      names.splice(pos, 0, moving);
      try {
        const res = await api('/api/panel/server/reorder', { ids: names });
        if (!res.ok) { toast('排序保存失败，请刷新重试'); return; }
        toast('服务器顺序已保存');
        await loadAllSystem(true);
      } catch (err) { toast('保存失败：' + err.message); }
    });
    host.addEventListener('dragend', () => resetSrvDrag(host));
  }

  // ------------------------------------------------------------ 事件
  document.getElementById('nav').addEventListener('click', (e) => {
    const b = e.target.closest('.nav-item');
    if (b) switchView(b.dataset.view);
  });

  $('trend-range').addEventListener('change', (e) => { trendRange = e.target.value; load(); });
  ['detail-range', 'detail-range-2'].forEach((id) => {
    $(id).addEventListener('click', (e) => {
      const b = e.target.closest('[data-r]');
      if (!b) return;
      detailRange = b.dataset.r;
      document.querySelectorAll('#detail-range button, #detail-range-2 button').forEach((x) => x.classList.toggle('on', x.dataset.r === detailRange));
      load();
    });
  });

  $('search').addEventListener('input', (e) => { keyword = e.target.value.trim(); renderTables(); });
  $('search2').addEventListener('input', (e) => { keyword2 = e.target.value.trim(); renderTables(); });
  $('filter-state').addEventListener('change', (e) => { filter = e.target.value; renderTables(); });

  document.addEventListener('click', async (e) => {
    const closer = e.target.closest('[data-close]');
    if (closer) {
      const target = $(closer.dataset.close);
      if (target) target.classList.add('hidden');
      return;
    }

    const groupCard = e.target.closest('[data-group-card]');
    if (groupCard) {
      selectedGroup = groupCard.dataset.groupCard;
      keyword2 = '';
      $('search2').value = '';
      renderGroupCards();
      renderTables();
      return;
    }

    const eye = e.target.closest('[data-eye]');
    if (eye) {
      showFullUrl = !showFullUrl;
      syncEyeButtons();
      renderTables();
      toast(showFullUrl ? '已显示完整网址' : '网址已隐藏');
      return;
    }

    const actBtn = e.target.closest('[data-act]');
    if (actBtn) {
      const row = actBtn.closest('tr');
      const id = row ? row.dataset.id : null;
      const srv = id ? D.servers.find((s) => s.id === id) : null;
      const act = actBtn.dataset.act;
      if (act === 'menu') {
        const pop = actBtn.parentElement.querySelector('.menu-list');
        const wasOpen = !pop.classList.contains('hidden');
        document.querySelectorAll('.menu-list').forEach((m) => m.classList.add('hidden'));
        if (!wasOpen) pop.classList.remove('hidden');
        return;
      }
      document.querySelectorAll('.menu-list').forEach((m) => m.classList.add('hidden'));
      if (act === 'detail' && srv) openDetail(id);
      else if (act === 'visit' && srv && !actBtn.disabled) window.open(safeUrl(srv.url), '_blank', 'noopener');
      else if (act === 'edit' && srv) openEdit(srv);
      else if (act === 'delete' && srv) {
        if (!confirm(`确定删除「${srv.name}」吗？\n仅移除监测项，不影响服务器本身。`)) return;
        try { await api('/api/server/delete', { id }); toast('已删除'); await load(); }
        catch (err) { toast('删除失败：' + err.message); }
      }
      return;
    }
    if (!e.target.closest('.menu-pop')) document.querySelectorAll('.menu-list').forEach((m) => m.classList.add('hidden'));
  });

  $('q-check').addEventListener('click', async () => {
    try { await api('/api/check', {}); toast('已触发全量检测'); setTimeout(() => load(true), 900); }
    catch (err) { toast('检测失败：' + err.message); }
  });
  $('q-refresh').addEventListener('click', () => { load(); toast('状态已刷新'); });
  $('q-logs').addEventListener('click', () => switchView('logs'));
  $('q-settings').addEventListener('click', () => switchView('settings'));
  $('btn-bell').addEventListener('click', () => switchView('logs'));
  $('btn-logout').addEventListener('click', async () => {
    try { await api('/api/logout', {}); } catch (_) { /* 忽略 */ }
    location.replace('/');
  });
  $('btn-add-site').addEventListener('click', () => openEdit(null));
  $('btn-edit-cancel').addEventListener('click', closeEdit);
  $('btn-edit-save').addEventListener('click', saveEdit);
  $('btn-dm-close').addEventListener('click', () => $('detail-modal').classList.add('hidden'));
  // 遮罩空白处不关闭弹窗：只认右上角 × / 取消 / 关闭按钮，以及 ESC
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeEdit(); $('detail-modal').classList.add('hidden'); $('srv-modal').classList.add('hidden'); }
  });
  $('f-url').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveEdit(); });

  $('btn-save-settings').addEventListener('click', async () => {
    try {
      await api('/api/config', {
        interval: Number($('set-interval').value) || 30,
        alert_sound: $('set-alert').checked,
      });
      toast('设置已保存'); await load(); settingsFilled = false;
    } catch (err) { toast('保存失败：' + err.message); }
  });
  $('btn-clear-logs').addEventListener('click', async () => {
    if (!confirm('确定清空全部日志记录吗？')) return;
    try { await api('/api/events/clear', {}); toast('日志已清空'); await load(); }
    catch (err) { toast('操作失败：' + err.message); }
  });

  // ---------------- 远端同步 ----------------
  if ($('btn-save-sync')) $('btn-save-sync').addEventListener('click', async () => {
    try {
      await api('/api/sync/config', {
        url: $('set-sync-url').value.trim(),
        user: $('set-sync-user').value.trim(),
        password: $('set-sync-pass').value,
        auto: $('set-sync-auto').checked,
        interval: Number($('set-sync-interval').value) || 30,
      });
      toast('同步设置已保存'); await load();
    } catch (err) { toast('保存失败：' + err.message); }
  });

  async function doSync(dir) {
    try {
      const r = await api('/api/sync', { direction: dir });
      if (r.ok) { toast(r.message || '同步完成'); await load(); }
      else toast('同步失败：' + (r.error || '未知错误'));
    } catch (err) { toast('同步失败：' + err.message); }
  }
  // 双向智能合并：两端都能改，同步时按修改时间取较新一方
  if ($('btn-sync-both')) $('btn-sync-both').addEventListener('click', () => doSync('both'));
  if ($('btn-sync-pull')) $('btn-sync-pull').addEventListener('click', () => doSync('pull'));
  if ($('btn-sync-push')) $('btn-sync-push').addEventListener('click', () => {
    if (!confirm('将把本地网址列表覆盖到公网端（http://your-server:7005），确认继续？')) return;
    doSync('push');
  });

  // ---------------- 风扇控制 ----------------
  async function setFanState(on) {
    if (!$('fan-enabled') || !$('fan-enabled').checked) { toast('请先在上方勾选「启用风扇控制」'); return; }
    const btn = on ? $('fan-on') : $('fan-off');
    if (btn) { btn.disabled = true; btn.style.opacity = '.6'; }
    try {
      const res = await api('/api/fan', {
        enabled: true,
        gpio: Number($('fan-gpio').value) || 18,
        inverted: $('fan-inverted').checked,
        state: on,
      });
      if (res.fan) D.fan = res.fan;
      if (res.temp != null) D.temp = res.temp;
      renderFan();
      const st = $('fan-status-msg');
      if (st) {
        st.textContent = res.message || (on ? '风扇已打开' : '风扇已关闭');
        st.className = 'fan-status ' + (res.ok === false ? 'bad' : 'ok');
      }
      if (res.ok !== false) toast(res.message || (on ? '风扇已打开' : '风扇已关闭'));
    } catch (e) {
      toast('操作失败：' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.style.opacity = ''; }
    }
  }

  async function saveFan() {
    try {
      const res = await api('/api/fan', {
        enabled: $('fan-enabled').checked,
        gpio: Number($('fan-gpio').value) || 18,
        inverted: $('fan-inverted').checked,
      });
      if (res.fan) D.fan = res.fan;
      if (res.temp != null) D.temp = res.temp;
      renderFan();
      toast('风扇设置已保存');
      await load(true);
    } catch (e) { toast('保存失败：' + e.message); }
  }

  if ($('fan-on')) $('fan-on').addEventListener('click', () => setFanState(true));
  if ($('fan-off')) $('fan-off').addEventListener('click', () => setFanState(false));
  if ($('btn-save-fan')) $('btn-save-fan').addEventListener('click', saveFan);
  if ($('fan-quick-on')) $('fan-quick-on').addEventListener('click', () => setFanState(true));
  if ($('fan-quick-off')) $('fan-quick-off').addEventListener('click', () => setFanState(false));

  if ($('sys-refresh')) $('sys-refresh').addEventListener('click', () => loadAllSystem(true));

  // 分组栏点击 → 切换分组，显示该组服务器
  if ($('sys-group-list')) $('sys-group-list').addEventListener('click', (e) => {
    const item = e.target.closest('.sys-group-item');
    if (item) selectGroup(item.getAttribute('data-group'));
  });
  // 中间服务器小卡点击 → 选中并显示详情
  if ($('sys-side-list')) $('sys-side-list').addEventListener('click', (e) => {
    const card = e.target.closest('.side-card');
    if (card) selectServer(card.getAttribute('data-id'));
  });
  // 详情区编辑
  if ($('sd-edit')) $('sd-edit').addEventListener('click', () => {
    const srv = statusServers.find((s) => s.id === selectedSrvId);
    if (srv) openServerModal(srv);
  });
  // 详情区删除
  if ($('sd-del')) $('sd-del').addEventListener('click', () => {
    const srv = statusServers.find((s) => s.id === selectedSrvId);
    if (!srv) return;
    if (!confirm('确定删除服务器「' + (srv.name || '') + '」吗？')) return;
    (async () => {
      try {
        await api('/api/panel/server/delete', { id: srv.id });
        toast('已删除');
        selectedSrvId = null;
        loadAllSystem(true);
      } catch (err) { toast('删除失败：' + err.message); }
    })();
  });

  function srvMsg(text, ok) {
    const el = $('sf-status');
    if (el) { el.style.display = 'block'; el.textContent = text; el.className = 'fan-status' + (ok === true ? ' ok' : ok === false ? ' bad' : ''); }
  }
  function openServerModal(srv) {
    $('sf-id').value = srv ? srv.id : '';
    $('sf-name').value = srv ? (srv.name || '') : '';
    $('sf-group').value = srv ? (srv.group || '默认分组') : (selectedSrvGroup || '');
    $('sf-type').value = srv ? (srv.type || 'agent') : 'agent';
    $('sf-base').value = srv ? (srv.base || '') : '';
    $('sf-key').value = '';   // 密钥不回显；编辑时留空保持原值需后端支持，这里保存时带回
    $('srv-modal-title').textContent = srv ? '编辑服务器' : '添加服务器';
    $('btn-srv-delete').style.display = srv ? 'inline-flex' : 'none';
    if ($('sf-status')) $('sf-status').style.display = 'none';
    $('srv-modal').classList.remove('hidden');
    setTimeout(() => $('sf-name').focus(), 50);
  }
  if ($('btn-srv-add')) $('btn-srv-add').addEventListener('click', () => openServerModal(null));
  if ($('btn-srv-save')) $('btn-srv-save').addEventListener('click', async () => {
    const name = $('sf-name').value.trim();
    const group = $('sf-group').value.trim();
    const base = $('sf-base').value.trim();
    const type = $('sf-type').value;
    const key = $('sf-key').value.trim();
    if (!name) { srvMsg('请填写服务器名称', false); return; }
    if (!base) { srvMsg('请填写 API 地址', false); return; }
    const id = $('sf-id').value;
    const payload = { name, group, base, type, key };
    if (id) payload.id = id;
    // 编辑时密钥留空，后端会保留原值
    try {
      const res = await api('/api/panel/server/save', payload);
      if (!res.ok) { srvMsg('保存失败：' + (res.error || '未知错误'), false); return; }
      $('srv-modal').classList.add('hidden');
      toast(id ? '服务器已更新' : '服务器已添加');
      selectedSrvGroup = group || '默认分组';
      selectedSrvId = (res.server && res.server.id) || id || null;
      loadAllSystem(true);
    } catch (e) { srvMsg('保存失败：' + e.message, false); }
  });
  if ($('btn-srv-delete')) $('btn-srv-delete').addEventListener('click', async () => {
    const id = $('sf-id').value;
    if (!id) return;
    const name = $('sf-name').value.trim() || '该服务器';
    if (!confirm('确定删除服务器「' + name + '」吗？')) return;
    try {
      await api('/api/panel/server/delete', { id });
      $('srv-modal').classList.add('hidden');
      toast('已删除');
      loadAllSystem(true);
    } catch (e) { srvMsg('删除失败：' + e.message, false); }
  });

  // ------------------------------------------------------------ 导出 JSON 备份
  if ($('btn-export')) $('btn-export').addEventListener('click', async () => {
    const msg = $('export-status-msg');
    if (msg) { msg.textContent = '正在生成备份…'; msg.className = 'fan-status'; }
    try {
      const r = await fetch('/api/export', { credentials: 'same-origin' });
      if (!r.ok) {
        if (r.status === 401) { location.replace('/'); return; }
        throw new Error('HTTP ' + r.status);
      }
      const blob = await r.blob();
      let name = 'panel-backup.json';
      const cd = r.headers.get('Content-Disposition') || '';
      const m = cd.match(/filename="([^"]+)"/);
      if (m) name = m[1];
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      if (msg) { msg.textContent = '备份已生成并开始下载：' + name; msg.className = 'fan-status ok'; }
      toast('备份已导出');
    } catch (e) {
      if (msg) { msg.textContent = '导出失败：' + e.message; msg.className = 'fan-status bad'; }
    }
  });

  // ------------------------------------------------------------ 导入 JSON 备份并恢复
  if ($('btn-import') && $('import-file')) {
    $('btn-import').addEventListener('click', () => $('import-file').click());
    $('import-file').addEventListener('change', async (e) => {
      const file = e.target.files && e.target.files[0];
      e.target.value = '';   // 允许重复选同一文件
      if (!file) return;
      const msg = $('export-status-msg');
      let backup;
      try {
        backup = JSON.parse(await file.text());
      } catch (err) {
        if (msg) { msg.textContent = '导入失败：不是有效的 JSON 文件'; msg.className = 'fan-status bad'; }
        return;
      }
      const nSites = Array.isArray(backup.servers) ? backup.servers.length : 0;
      const nSrv = Array.isArray(backup.status_servers) ? backup.status_servers.length : 0;
      if (!nSites && !nSrv) {
        if (msg) { msg.textContent = '导入失败：备份里没有网址或服务器状态数据'; msg.className = 'fan-status bad'; }
        return;
      }
      if (!confirm(`确定用该备份恢复配置吗？\n网址 ${nSites} 条、服务器状态 ${nSrv} 台。\n将覆盖当前的网址清单和服务器状态列表。`)) return;
      if (msg) { msg.textContent = '正在恢复…'; msg.className = 'fan-status'; }
      try {
        const res = await api('/api/import', { backup });
        if (!res.ok) throw new Error(res.error || '恢复失败');
        if (msg) { msg.textContent = `恢复成功：网址 ${res.servers != null ? res.servers : '未变'} 条，服务器状态 ${res.status_servers != null ? res.status_servers : '未变'} 台`; msg.className = 'fan-status ok'; }
        toast('备份已恢复');
        await load(true);   // 刷新网址数据
        loadAllSystem(true); // 刷新服务器状态数据
      } catch (err) {
        if (msg) { msg.textContent = '恢复失败：' + err.message; msg.className = 'fan-status bad'; }
      }
    });
  }

  // ------------------------------------------------------------ 启动
  (async function init() {
    initDrag();
    initGroupDrag();
    initStatusDrag();
    syncEyeButtons();
    await load(true);
    fillFanForm();
    renderFan();
    setInterval(() => load(true), 4000);
    // 服务器状态每 3 秒刷新一次（后端缓存已降到 2 秒，链路总延迟约 2-3 秒）
    setInterval(() => { if (view === 'system') loadAllSystem(false); }, 3000);
  })();
})();
