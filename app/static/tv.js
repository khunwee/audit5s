/* 5ส Vision: จอแสดงผล (TV)
   เขียนแบบ ES5 ไม่ใช้ไลบรารีภายนอก เพื่อให้เบราว์เซอร์ของ TV รุ่นเก่าทำงานได้
   การตั้งค่า 3 ชั้น: ค่ากลางจากผู้ดูแล < ค่าที่บันทึกไว้ในจอนี้ (localStorage) < ค่าที่ต่อท้ายลิงก์ (?lang=en&theme=dark...) */
(function () {
  'use strict';

  var SERVER = JSON.parse(document.getElementById('tv-config').textContent);
  var STORE = 'tv5s.settings';
  var ALL = ['ranking', 'trend', 'ranks', 'history', 'category', 'actions'];

  var T = {
    th: {
      ranking: 'อันดับ 5ส', trend: 'แนวโน้มคะแนน', ranks: 'ประวัติอันดับ', history: 'คะแนนย้อนหลัง',
      category: 'คะแนนรายหมวดและข้อที่ไม่ผ่านบ่อย', actions: 'งานแก้ไข',
      loading: 'กำลังโหลดข้อมูล', noRound: 'ยังไม่มีรอบการตรวจที่แสดงผลได้', noRank: 'ยังไม่มีแผนกที่ถูกจัดอันดับในรอบนี้',
      denied: 'ลิงก์ของจอนี้ใช้ไม่ได้แล้ว ขอลิงก์ใหม่จากผู้ดูแลระบบ',
      photos: 'ภาพ', vsPrev: 'เทียบรอบก่อน', first: 'รอบแรก', up: 'ขึ้น {n} อันดับ', down: 'ลง {n} อันดับ', same: 'อันดับเดิม',
      unranked: 'ยังไม่ถูกจัดอันดับ', updated: 'ข้อมูลเมื่อ', offline: 'ต่อระบบไม่ได้ แสดงข้อมูลล่าสุดเมื่อ',
      green: 'ตั้งแต่ {g}%', yellow: '{m} ถึง {h}%', red: 'ต่ำกว่า {m}%',
      live: 'รอบเปิดอยู่ คะแนนยังเปลี่ยนได้', final: 'ผลสุดท้ายของรอบ', rank: 'อันดับ', dept: 'แผนก', change: 'เปลี่ยนแปลง',
      catTitle: 'คะแนนเฉลี่ยรายหมวด ทั้งโรงงาน', parTitle: 'ข้อที่ไม่ผ่านบ่อยที่สุด', times: 'ครั้ง', points: 'จุดตรวจ',
      noFindings: 'ยังไม่พบข้อที่ไม่ผ่านในรอบนี้', levelMode: 'รอบนี้ให้คะแนนแบบระดับ จึงไม่มีผลรายข้อ',
      actOpen: 'งานแก้ไขเปิดอยู่', actOver: 'เกินกำหนด', actDept: 'งานแก้ไขรายแผนก', noActions: 'ไม่มีงานแก้ไขค้าง',
      avg: 'เฉลี่ยทั้งโรงงาน', depGood: 'แผนกสีเขียว', depMid: 'แผนกสีเหลือง', depLow: 'แผนกสีแดง', now: 'ล่าสุด',
      settings: 'ตั้งค่าจอนี้', language: 'ภาษา', theme: 'สีพื้นของจอ', light: 'พื้นสว่าง', dark: 'พื้นเข้ม',
      seconds: 'เวลาต่อหนึ่งหน้า (วินาที)', rows: 'จำนวนแผนกต่อหน้า', rounds: 'จำนวนรอบย้อนหลังในกราฟ', pages: 'หน้าที่แสดง',
      clock: 'แสดงนาฬิกา', unrankedOpt: 'แสดงแผนกที่ยังไม่ถูกจัดอันดับ', save: 'บันทึกสำหรับจอนี้', reset: 'ใช้ค่ากลางของผู้ดูแล', close: 'ปิด',
      panel_note: 'ค่าที่บันทึกเก็บไว้ในเบราว์เซอร์ของจอนี้เท่านั้น ปุ่มลัด: F เต็มจอ, L เปลี่ยนภาษา, ลูกศรเปลี่ยนหน้า, เว้นวรรคหยุดหรือเล่นต่อ',
      fullOn: 'เต็มจอ', fullOff: 'ออกจากเต็มจอ'
    },
    en: {
      ranking: '5S ranking', trend: 'Score trend', ranks: 'Rank history', history: 'Score history',
      category: 'Category scores and top findings', actions: 'Corrective actions',
      loading: 'Loading data', noRound: 'No audit round to display yet', noRank: 'No department is ranked in this round yet',
      denied: 'This display link is no longer valid. Ask the administrator for a new one.',
      photos: 'photos', vsPrev: 'vs previous round', first: 'First round', up: 'Up {n}', down: 'Down {n}', same: 'Same rank',
      unranked: 'Not ranked yet', updated: 'Data as of', offline: 'Offline. Showing data from',
      green: '{g}% and above', yellow: '{m} to {h}%', red: 'Below {m}%',
      live: 'Round is open; scores can still change', final: 'Final result of the round', rank: 'Rank', dept: 'Department', change: 'Change',
      catTitle: 'Average score by category, whole factory', parTitle: 'Most frequent findings', times: 'times', points: 'audit points',
      noFindings: 'No findings in this round yet', levelMode: 'This round uses level scoring, so there are no item results',
      actOpen: 'Open actions', actOver: 'Overdue', actDept: 'Corrective actions by department', noActions: 'No open corrective actions',
      avg: 'Factory average', depGood: 'Green departments', depMid: 'Yellow departments', depLow: 'Red departments', now: 'Latest',
      settings: 'Settings for this screen', language: 'Language', theme: 'Background', light: 'Light', dark: 'Dark',
      seconds: 'Seconds per page', rows: 'Departments per page', rounds: 'Rounds of history in charts', pages: 'Pages to show',
      clock: 'Show clock', unrankedOpt: 'Show departments not ranked yet', save: 'Save for this screen', reset: 'Use administrator defaults', close: 'Close',
      panel_note: 'Saved settings stay in this screen\'s browser only. Shortcuts: F full screen, L language, arrows change page, space pause or play.',
      fullOn: 'Full screen', fullOff: 'Exit full screen'
    }
  };
  var S_KEYS = { s_ranking: 'ranking', s_trend: 'trend', s_ranks: 'ranks', s_history: 'history', s_category: 'category', s_actions: 'actions', unranked: 'unrankedOpt' };
  var LINES = {
    light: ['#1F7A4D', '#2A5DA8', '#C8321F', '#B98A00', '#6B3FA0', '#0F8B8D', '#D4621A', '#4E5D94', '#8A3B62', '#5B7F2B'],
    dark: ['#3FB377', '#7CA8EA', '#EA5C45', '#F2B705', '#B08AE0', '#3CC5C7', '#F08A45', '#9AA8E0', '#E07BA8', '#A4CF5B']
  };

  // ------------------------------------------------------------ การตั้งค่า
  function stored() { try { return JSON.parse(window.localStorage.getItem(STORE) || '{}') || {}; } catch (e) { return {}; } }
  function fromQuery() {
    var out = {}, q = window.location.search.replace(/^\?/, '').split('&');
    for (var i = 0; i < q.length; i++) {
      var kv = q[i].split('='), k = kv[0], v = decodeURIComponent(kv[1] || '');
      if (k === 'lang' && (v === 'th' || v === 'en')) out.lang = v;
      if (k === 'theme' && (v === 'light' || v === 'dark')) out.theme = v;
      if (k === 'sec' && +v) out.seconds = +v;
      if (k === 'rows' && +v) out.rows = +v;
      if (k === 'rounds' && +v) out.rounds = +v;
      if (k === 'slides' && v) out.slides = v.split(',');
    }
    return out;
  }
  function clean(c) {
    c.lang = c.lang === 'en' ? 'en' : 'th';
    c.theme = c.theme === 'dark' ? 'dark' : 'light';
    c.seconds = Math.max(5, Math.min(300, parseInt(c.seconds, 10) || 15));
    c.rows = Math.max(3, Math.min(20, parseInt(c.rows, 10) || 8));
    c.rounds = Math.max(2, Math.min(12, parseInt(c.rounds, 10) || 6));
    c.refresh = Math.max(1, parseInt(c.refresh, 10) || 5);
    var keep = [];
    for (var i = 0; i < (c.slides || []).length; i++) if (ALL.indexOf(c.slides[i]) >= 0 && keep.indexOf(c.slides[i]) < 0) keep.push(c.slides[i]);
    c.slides = keep.length ? keep : ['ranking'];
    return c;
  }
  function merged() {
    var c = {}, layers = [SERVER, stored(), fromQuery()], k;
    for (var i = 0; i < layers.length; i++) for (k in layers[i]) if (Object.prototype.hasOwnProperty.call(layers[i], k)) c[k] = layers[i][k];
    return clean(c);
  }
  var cfg = merged();

  // ------------------------------------------------------------ เครื่องมือเล็ก ๆ
  function $(id) { return document.getElementById(id); }
  function t(key, vars) {
    var s = (T[cfg.lang] && T[cfg.lang][key]) || T.th[key] || key;
    if (vars) for (var k in vars) s = s.replace('{' + k + '}', vars[k]);
    return s;
  }
  function esc(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function name(o) { return cfg.lang === 'en' && o.name_en ? o.name_en : o.name; }
  function num(v) { return v == null ? '-' : (Math.round(v * 10) / 10).toString(); }
  function chunk(list, n) { var out = []; for (var i = 0; i < list.length; i += n) out.push(list.slice(i, i + n)); return out; }
  function bandOf(v) { return v == null ? 'none' : v >= data.bands.good ? 'good' : v >= data.bands.mid ? 'mid' : 'low'; }
  function rem() { return parseFloat(window.getComputedStyle(document.documentElement).fontSize) || 16; }
  function cut(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

  var data = null, slides = [], index = 0, timer = null, paused = false, offline = false, lastOk = '';
  var stage = $('tv-stage');

  // ------------------------------------------------------------ ขนาดตัวอักษรตามขนาดจอ
  function fit() {
    var w = window.innerWidth, h = window.innerHeight, portrait = h > w * 1.1 || w < 700;
    var size = portrait ? Math.max(11, Math.min(w / 30, h / 52)) : Math.max(9, Math.min(w / 80, h / 45));
    document.documentElement.style.fontSize = size + 'px';
    document.body.className = document.body.className.replace(/\s*portrait/g, '') + (portrait ? ' portrait' : '');
    return portrait;
  }

  // ------------------------------------------------------------ หน้าต่าง ๆ
  function deltaHtml(r) {
    if (r.delta == null) return '<span class="flat">' + t('first') + '</span>';
    var cls = r.delta > 0.04 ? 'up' : r.delta < -0.04 ? 'down' : 'flat';
    var arrow = cls === 'up' ? '▲ +' : cls === 'down' ? '▼ ' : '● ';
    var rk = r.rank_delta == null ? '' : r.rank_delta > 0 ? t('up', { n: r.rank_delta }) : r.rank_delta < 0 ? t('down', { n: -r.rank_delta }) : t('same');
    return '<span class="' + cls + '">' + arrow + num(r.delta) + '</span><small>' + (rk || t('vsPrev')) + '</small>';
  }

  function renderRanking(el, rows, last) {
    if (!data.rows.length) { el.innerHTML = '<p class="tv-msg">' + t('noRank') + '</p>'; return; }
    var html = '<div class="rk" style="grid-template-rows:repeat(' + cfg.rows + ',1fr)">';
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      html += '<div class="rk-row ' + r.band + (r.rank <= 3 ? ' top' : '') + '">' +
        '<div class="rk-n">' + r.rank + '</div>' +
        '<div class="rk-name">' + esc(name(r)) + '<small>' + r.scored + ' ' + t('photos') + '</small></div>' +
        '<div class="rk-track"><i style="width:' + Math.max(0, Math.min(100, r.avg)) + '%"></i>' +
        '<b style="left:' + data.bands.mid + '%"></b><b style="left:' + data.bands.good + '%"></b></div>' +
        '<div class="rk-score">' + num(r.avg) + '<small>%</small></div>' +
        '<div class="rk-delta">' + deltaHtml(r) + '</div></div>';
    }
    html += '</div>';
    if (last && cfg.unranked && data.unranked.length) {
      var names = [];
      for (var j = 0; j < data.unranked.length; j++) names.push(esc(name(data.unranked[j])));
      html += '<p class="rk-foot"><b>' + t('unranked') + ':</b> ' + names.join(', ') + '</p>';
    }
    el.innerHTML = html;
  }

  function allValues() {
    var lo = 100, s = data.history.series;
    for (var i = 0; i < s.length; i++) for (var j = 0; j < s[i].avg.length; j++) if (s[i].avg[j] != null && s[i].avg[j] < lo) lo = s[i].avg[j];
    return Math.max(0, Math.floor((lo - 8) / 10) * 10);
  }

  function spark(box, values, cls) {
    var w = box.clientWidth, h = box.clientHeight, u = rem();
    if (w < 20 || h < 20) return;
    var lo = allValues(), hi = 100, n = values.length, padX = u * 0.6, padT = u * 0.9, padB = u * 0.5;
    var x = function (i) { return n < 2 ? w / 2 : padX + i * (w - 2 * padX) / (n - 1); };
    var y = function (v) { return padT + (hi - v) / (hi - lo) * (h - padT - padB); };
    var color = 'var(--' + (cls === 'good' ? 'go' : cls === 'mid' ? 'mid' : cls === 'low' ? 'tag' : 'ink2') + ')';
    var out = '<svg viewBox="0 0 ' + w + ' ' + h + '" aria-hidden="true">';
    var g = data.bands.good;
    if (g > lo) out += '<line class="grid" x1="0" x2="' + w + '" y1="' + y(g) + '" y2="' + y(g) + '" stroke-width="' + u * 0.06 + '" stroke-dasharray="' + u * 0.3 + ' ' + u * 0.3 + '"/>';
    var path = '', open = false, lastI = -1;
    for (var i = 0; i < n; i++) {
      if (values[i] == null) { open = false; continue; }
      path += (open ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(values[i]).toFixed(1);
      open = true; lastI = i;
    }
    out += '<path d="' + path + '" fill="none" stroke="' + color + '" stroke-width="' + u * 0.18 + '" stroke-linejoin="round" stroke-linecap="round"/>';
    for (i = 0; i < n; i++) {
      if (values[i] == null) continue;
      out += '<circle cx="' + x(i).toFixed(1) + '" cy="' + y(values[i]).toFixed(1) + '" r="' + (i === lastI ? u * 0.34 : u * 0.2) + '" fill="' + color + '"/>';
    }
    box.innerHTML = out + '</svg>';
  }

  function renderTrend(el, list, portrait) {
    var cols = portrait ? 2 : 4, rowsN = Math.max(1, Math.ceil(list.length / cols));
    var html = '<div class="tr" style="grid-template-columns:repeat(' + cols + ',1fr);grid-template-rows:repeat(' + (portrait ? 3 : Math.max(2, rowsN)) + ',1fr)">';
    for (var i = 0; i < list.length; i++) {
      var s = list[i], n = s.avg.length, cur = s.avg[n - 1], first = null, ranks = [];
      for (var j = 0; j < n; j++) { if (first == null && s.avg[j] != null) first = s.avg[j]; if (s.rank[j] != null) ranks.push(s.rank[j]); }
      var d = cur != null && first != null && n > 1 ? cur - first : null;
      var dc = d == null ? 'flat' : d > 0.04 ? 'up' : d < -0.04 ? 'down' : 'flat';
      html += '<div class="tr-card ' + bandOf(cur) + '"><div class="tr-top"><span class="tr-name">' + esc(name(s)) + '</span>' +
        '<span class="tr-score">' + num(cur) + (cur == null ? '' : '%') + '</span></div>' +
        '<div class="tr-sub"><span>' + t('rank') + ' ' + (ranks.length ? ranks.slice(-4).join(' → ') : '-') + '</span>' +
        '<span class="' + dc + '">' + (d == null ? '' : (d > 0 ? '+' : '') + num(d)) + '</span></div>' +
        '<div class="tr-plot" data-i="' + i + '"></div></div>';
    }
    el.innerHTML = html + '</div>';
    var plots = el.querySelectorAll('.tr-plot');
    for (i = 0; i < plots.length; i++) spark(plots[i], list[i].avg, bandOf(list[i].avg[list[i].avg.length - 1]));
  }

  function renderRanks(el, list) {
    el.innerHTML = '<div class="chart"></div>';
    var box = el.firstChild, w = box.clientWidth - 2, h = box.clientHeight - 2, u = rem();
    if (w < 50 || h < 50) return;
    var rounds = data.history.rounds, n = rounds.length, all = data.history.series, maxRank = 1, i, j;
    for (i = 0; i < all.length; i++) for (j = 0; j < n; j++) if (all[i].rank[j] > maxRank) maxRank = all[i].rank[j];
    var left = u * 3.2, right = Math.min(w * 0.3, u * 16), top = u * 1.4, bottom = u * 3;
    var pw = w - left - right, ph = h - top - bottom;
    var x = function (k) { return n < 2 ? left + pw / 2 : left + k * pw / (n - 1); };
    var y = function (r) { return maxRank < 2 ? top + ph / 2 : top + (r - 1) * ph / (maxRank - 1); };
    var colors = LINES[cfg.theme], out = '<svg viewBox="0 0 ' + w + ' ' + h + '" role="img">';
    var step = maxRank > 12 ? 2 : 1;
    for (var r = 1; r <= maxRank; r += step) {
      out += '<line class="grid" x1="' + left + '" x2="' + (left + pw) + '" y1="' + y(r) + '" y2="' + y(r) + '" stroke-width="' + u * 0.05 + '"/>' +
        '<text class="quiet" x="' + (left - u * 0.9) + '" y="' + (y(r) + u * 0.3) + '" text-anchor="end" font-size="' + u * 0.85 + '">' + r + '</text>';
    }
    var maxLen = Math.max(6, Math.floor(pw / Math.max(1, n - 1) / (u * 0.5)));
    for (j = 0; j < n; j++) {
      out += '<text class="quiet" x="' + x(j) + '" y="' + (h - u * 1.2) + '" text-anchor="' + (j === 0 ? 'start' : j === n - 1 ? 'end' : 'middle') + '" font-size="' + u * 0.8 + '">' + esc(cut(rounds[j].name, Math.min(22, maxLen))) + '</text>';
    }
    var labels = [];
    for (i = 0; i < list.length; i++) {
      var s = list[i], c = colors[i % colors.length], path = '', open = false, lastJ = -1;
      for (j = 0; j < n; j++) {
        if (s.rank[j] == null) { open = false; continue; }
        path += (open ? 'L' : 'M') + x(j).toFixed(1) + ' ' + y(s.rank[j]).toFixed(1);
        open = true; lastJ = j;
      }
      if (lastJ < 0) continue;
      out += '<path d="' + path + '" fill="none" stroke="' + c + '" stroke-width="' + u * 0.24 + '" stroke-linejoin="round" stroke-linecap="round"/>';
      for (j = 0; j < n; j++) if (s.rank[j] != null) out += '<circle cx="' + x(j).toFixed(1) + '" cy="' + y(s.rank[j]).toFixed(1) + '" r="' + u * 0.38 + '" fill="' + c + '"/>';
      labels.push({ y: y(s.rank[lastJ]), x: x(lastJ), text: s.rank[lastJ] + '  ' + name(s), c: c });
    }
    labels.sort(function (a, b) { return a.y - b.y; });
    var gap = u * 1.35;
    for (i = 1; i < labels.length; i++) if (labels[i].y - labels[i - 1].y < gap) labels[i].y = labels[i - 1].y + gap;
    var maxChars = Math.max(8, Math.floor((right - u * 1.6) / (u * 0.56)));
    for (i = 0; i < labels.length; i++) {
      out += '<text x="' + (left + pw + u * 1.1) + '" y="' + (labels[i].y + u * 0.34) + '" font-size="' + u * 0.98 + '" font-weight="600" style="fill:' + labels[i].c + '">' + esc(cut(labels[i].text, maxChars)) + '</text>';
    }
    box.innerHTML = out + '</svg>';
  }

  function renderHistory(el, list) {
    var rounds = data.history.rounds, n = rounds.length;
    var cols = 'minmax(9rem,18rem) repeat(' + n + ',1fr) 6.5rem';
    var html = '<div class="hist" style="grid-template-rows:2.6rem repeat(' + (cfg.rows + 2) + ',1fr)"><div class="hist-row hist-head" style="grid-template-columns:' + cols + '"><div class="hist-cell hist-name">' + t('dept') + '</div>';
    for (var j = 0; j < n; j++) html += '<div class="hist-cell">' + esc(cut(rounds[j].name, 26)) + '</div>';
    html += '<div class="hist-cell">' + t('change') + '</div></div>';
    for (var i = 0; i < list.length; i++) {
      var s = list[i], first = null, cur = s.avg[n - 1];
      html += '<div class="hist-row" style="grid-template-columns:' + cols + '"><div class="hist-cell hist-name"><span>' + esc(name(s)) + '</span></div>';
      for (j = 0; j < n; j++) {
        var v = s.avg[j];
        if (first == null && v != null) first = v;
        html += '<div class="hist-cell ' + bandOf(v) + '">' + num(v) + (v != null && s.rank[j] ? '<small>#' + s.rank[j] + '</small>' : '') + '</div>';
      }
      var d = cur != null && first != null && n > 1 ? cur - first : null;
      html += '<div class="hist-cell ' + (d == null ? 'none' : d > 0.04 ? 'up' : d < -0.04 ? 'down' : 'flat') + '">' + (d == null ? '-' : (d > 0 ? '+' : '') + num(d)) + '</div></div>';
    }
    el.innerHTML = html + '</div>';
  }

  function renderCategory(el) {
    var html = '<div class="two"><div class="box"><h2>' + t('catTitle') + '</h2><div class="box-body">';
    for (var i = 0; i < data.cats.length; i++) {
      var c = data.cats[i], v = c.avg;
      html += '<div class="cat-row"><span>' + esc(cfg.lang === 'en' && c.name_en ? c.name_en : c.name) + '</span>' +
        '<div class="rk-track"><i class="bg-' + c.band + '" style="width:' + (v == null ? 0 : v) + '%"></i></div><b>' + num(v) + (v == null ? '' : '%') + '</b></div>';
    }
    html += '</div></div><div class="box"><h2>' + t('parTitle') + '</h2><div class="box-body">';
    if (!data.checklist) html += '<p class="tv-msg">' + t('levelMode') + '</p>';
    else if (!data.pareto.length) html += '<p class="tv-msg">' + t('noFindings') + '</p>';
    else {
      var top = data.pareto[0].count || 1;
      for (i = 0; i < data.pareto.length; i++) {
        var p = data.pareto[i];
        html += '<div class="par-row"><p><b>' + esc(p.code) + '</b>' + esc(cfg.lang === 'en' && p.text_en ? p.text_en : p.text) + '</p>' +
          '<small>' + p.count + ' ' + t('times') + ', ' + p.areas + ' ' + t('points') + '</small>' +
          '<div class="par-bar"><i class="mj" style="width:' + (p.major / top * 100) + '%"></i><i class="mn" style="width:' + (p.minor / top * 100) + '%"></i></div></div>';
      }
    }
    el.innerHTML = html + '</div></div></div>';
  }

  function renderActions(el, list) {
    var s = data.summary, a = data.actions;
    var html = '<div class="tiles"><div class="tile"><b>' + a.open + '</b><span>' + t('actOpen') + '</span></div>' +
      '<div class="tile low"><b>' + a.overdue + '</b><span>' + t('actOver') + '</span></div>' +
      '<div class="tile ' + bandOf(s.avg) + '"><b>' + num(s.avg) + (s.avg == null ? '' : '%') + '</b><span>' + t('avg') + '</span></div>' +
      '<div class="tile good"><b>' + s.good + ' / ' + s.mid + ' / ' + s.low + '</b><span>' + t('depGood') + ' / ' + t('depMid').split(' ').pop() + ' / ' + t('depLow').split(' ').pop() + '</span></div></div>';
    var cols = 'minmax(9rem,1fr) 9rem 9rem';
    html += '<div class="hist" style="grid-template-rows:2.4rem repeat(' + cfg.rows + ',1fr)"><div class="hist-row hist-head" style="grid-template-columns:' + cols + '"><div class="hist-cell hist-name">' + t('dept') + '</div><div class="hist-cell">' + t('actOpen') + '</div><div class="hist-cell">' + t('actOver') + '</div></div>';
    for (var i = 0; i < list.length; i++) {
      html += '<div class="hist-row" style="grid-template-columns:' + cols + '"><div class="hist-cell hist-name"><span>' + esc(name(list[i])) + '</span></div>' +
        '<div class="hist-cell">' + list[i].open + '</div><div class="hist-cell ' + (list[i].overdue ? 'low down' : 'none') + '">' + list[i].overdue + '</div></div>';
    }
    el.innerHTML = html + '</div>';
  }

  // ------------------------------------------------------------ สร้างรายการหน้าและหมุนหน้า
  function build() {
    slides = [];
    if (!data || !data.round) return;
    var portrait = /portrait/.test(document.body.className), hasHistory = data.history.rounds.length > 1;
    var add = function (key, pages, fn) {
      for (var i = 0; i < pages.length; i++) (function (p, i) { slides.push({ key: key, page: i + 1, pages: pages.length, draw: function (el) { fn(el, p, i === pages.length - 1); } }); })(pages[i], i);
    };
    for (var k = 0; k < cfg.slides.length; k++) {
      var key = cfg.slides[k];
      if (key === 'ranking') add(key, data.rows.length ? chunk(data.rows, cfg.rows) : [[]], renderRanking);
      if (key === 'trend' && hasHistory && data.history.series.length) add(key, chunk(data.history.series, portrait ? 6 : 12), function (el, p) { renderTrend(el, p, portrait); });
      if (key === 'ranks' && hasHistory && data.rows.length) {
        var ranked = [];
        for (var i = 0; i < data.history.series.length; i++) { var sr = data.history.series[i]; for (var j = 0; j < sr.rank.length; j++) if (sr.rank[j] != null) { ranked.push(sr); break; } }
        if (ranked.length) add(key, chunk(ranked, 10), renderRanks);
      }
      if (key === 'history' && data.history.series.length) add(key, chunk(data.history.series, cfg.rows + 2), renderHistory);
      if (key === 'category' && data.cats.length) add(key, [null], renderCategory);
      if (key === 'actions' && data.actions.open > 0) {
        var withActs = [];
        for (i = 0; i < data.rows.length; i++) if (data.rows[i].open) withActs.push(data.rows[i]);
        withActs.sort(function (a, b) { return b.overdue - a.overdue || b.open - a.open; });
        add(key, withActs.length ? chunk(withActs, cfg.rows) : [[]], renderActions);
      }
    }
  }

  function chrome() {
    document.documentElement.lang = cfg.lang;
    document.body.className = document.body.className.replace(/theme-\w+/, 'theme-' + cfg.theme);
    $('tv-title').textContent = cfg.lang === 'en' ? (cfg.title_en || cfg.title) : cfg.title;
    $('tv-lang').textContent = cfg.lang === 'en' ? 'ไทย' : 'EN';
    $('tv-clock').style.display = cfg.clock ? '' : 'none';
    var r = data && data.round;
    $('tv-round').textContent = r ? r.name + (r.start || r.end ? '  (' + [r.start, r.end].join(' - ') + ')' : '') + '  ' + (r.status === 'open' ? t('live') : t('final')) : '';
    if (data) {
      var b = data.bands;
      $('tv-legend').innerHTML = '<span><i class="bg-good"></i>' + t('green', { g: b.good }) + '</span><span><i class="bg-mid"></i>' + t('yellow', { m: b.mid, h: b.good - 1 }) + '</span><span><i class="bg-low"></i>' + t('red', { m: b.mid }) + '</span>';
      $('tv-updated').textContent = (offline ? t('offline') : t('updated')) + ' ' + (offline ? lastOk : data.generated);
      $('tv-updated').className = 'tv-updated' + (offline ? ' off' : '');
    }
    var nodes = document.querySelectorAll('[data-t]');
    for (var i = 0; i < nodes.length; i++) { var key = nodes[i].getAttribute('data-t'); nodes[i].textContent = t(S_KEYS[key] || key); }
  }

  function show(i) {
    window.clearTimeout(timer);
    chrome();
    var bar = $('tv-bar');
    bar.style.transition = 'none'; bar.style.width = '0';
    if (!slides.length) {
      stage.innerHTML = '<p class="tv-msg">' + t(data ? 'noRound' : 'loading') + '</p>';
      $('tv-slide').textContent = ''; $('tv-dots').innerHTML = '';
      return;
    }
    index = (i + slides.length) % slides.length;
    var s = slides[index];
    stage.innerHTML = '';
    var el = document.createElement('div');
    el.style.cssText = 'flex:1;min-height:0;display:flex;flex-direction:column';
    stage.appendChild(el);
    s.draw(el);
    $('tv-slide').textContent = t(s.key) + (s.pages > 1 ? '  ' + s.page + '/' + s.pages : '');
    var dots = '';
    for (var d = 0; d < slides.length && slides.length <= 24; d++) dots += '<i' + (d === index ? ' class="on"' : '') + '></i>';
    $('tv-dots').innerHTML = dots;
    if (!paused && slides.length > 1) {
      void bar.offsetWidth;
      bar.style.transition = 'width ' + cfg.seconds + 's linear'; bar.style.width = '100%';
      timer = window.setTimeout(function () { show(index + 1); }, cfg.seconds * 1000);
    }
  }

  // ------------------------------------------------------------ ดึงข้อมูล
  function inHours() {
    var m = /^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$/.exec(cfg.hours || '');
    if (!m) return true;
    var now = new Date(), cur = now.getHours() * 60 + now.getMinutes(), a = +m[1] * 60 + +m[2], b = +m[3] * 60 + +m[4];
    return a <= b ? (cur >= a && cur <= b) : (cur >= a || cur <= b);
  }
  function fetchData(force) {
    if (!force && data && !inHours()) return;
    var x = new XMLHttpRequest();
    x.open('GET', '/api/tv/data?rounds=' + cfg.rounds, true);
    x.timeout = 60000;
    x.onload = function () {
      if (x.status === 403) { data = null; slides = []; stage.innerHTML = '<p class="tv-msg">' + t('denied') + '</p>'; return; }
      if (x.status !== 200) { fail(); return; }
      var fresh;
      try { fresh = JSON.parse(x.responseText); } catch (e) { fail(); return; }
      var changed = !data || JSON.stringify(fresh) !== JSON.stringify(data);
      data = fresh; offline = false; lastOk = data.generated;
      if (changed) { var keep = index; build(); show(Math.min(keep, Math.max(0, slides.length - 1))); } else chrome();
    };
    x.onerror = x.ontimeout = fail;
    x.send();
  }
  function fail() { offline = true; if (data) chrome(); else stage.innerHTML = '<p class="tv-msg">' + t('loading') + '</p>'; }

  // ------------------------------------------------------------ เต็มจอ ปุ่มควบคุม แผงตั้งค่า
  function isFull() { return !!(document.fullscreenElement || document.webkitFullscreenElement); }
  function toggleFull() {
    var el = document.documentElement;
    try {
      if (isFull()) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      else (el.requestFullscreen || el.webkitRequestFullscreen).call(el);
    } catch (e) { /* TV บางรุ่นไม่มีคำสั่งเต็มจอ ใช้ปุ่มเต็มจอของเบราว์เซอร์แทน */ }
  }
  function redraw() { fit(); build(); show(Math.min(index, Math.max(0, slides.length - 1))); }
  function saveLocal(c) { try { window.localStorage.setItem(STORE, JSON.stringify(c)); } catch (e) { /* จอที่ปิดการเก็บข้อมูล: ใช้ค่าต่อท้ายลิงก์แทน */ } }
  function setLang(lang) { var s = stored(); s.lang = lang; saveLocal(s); cfg.lang = lang; redraw(); }

  var idle = null;
  function wake() {
    if (!/show-ctrl/.test(document.body.className)) document.body.className += ' show-ctrl';
    window.clearTimeout(idle);
    idle = window.setTimeout(function () { if ($('tv-panel').hidden) document.body.className = document.body.className.replace(/\s*show-ctrl/g, ''); }, 5000);
  }
  ['mousemove', 'mousedown', 'touchstart', 'keydown'].forEach(function (e) { document.addEventListener(e, wake, false); });

  function openPanel() {
    var f = $('tv-form');
    f.lang.value = cfg.lang; f.theme.value = cfg.theme; f.seconds.value = cfg.seconds; f.rows.value = cfg.rows; f.rounds.value = cfg.rounds;
    f.clock.checked = !!cfg.clock; f.unranked.checked = !!cfg.unranked;
    var boxes = f.querySelectorAll('input[name=slides]');
    for (var i = 0; i < boxes.length; i++) boxes[i].checked = cfg.slides.indexOf(boxes[i].value) >= 0;
    $('tv-panel').hidden = false; wake();
  }
  $('tv-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var f = e.target, picked = [], boxes = f.querySelectorAll('input[name=slides]');
    for (var i = 0; i < boxes.length; i++) if (boxes[i].checked) picked.push(boxes[i].value);
    var old = cfg.rounds;
    saveLocal(clean({ lang: f.lang.value, theme: f.theme.value, seconds: f.seconds.value, rows: f.rows.value, rounds: f.rounds.value,
      slides: picked, clock: f.clock.checked, unranked: f.unranked.checked, refresh: cfg.refresh }));
    cfg = merged(); $('tv-panel').hidden = true;
    if (cfg.rounds !== old) fetchData(true);
    redraw();
  });
  $('tv-panel').addEventListener('click', function (e) {
    var act = e.target.getAttribute && e.target.getAttribute('data-act');
    if (act === 'close') $('tv-panel').hidden = true;
    if (act === 'reset') { try { window.localStorage.removeItem(STORE); } catch (err) { /* ไม่มีที่เก็บ */ } cfg = merged(); $('tv-panel').hidden = true; fetchData(true); redraw(); }
  });
  $('tv-ctrl').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button') : e.target, act = b && b.getAttribute('data-act');
    if (act === 'prev') show(index - 1);
    if (act === 'next') show(index + 1);
    if (act === 'pause') { paused = !paused; $('tv-pause').innerHTML = paused ? '&#9654;&#9654;' : '&#10073;&#10073;'; show(index); }
    if (act === 'lang') setLang(cfg.lang === 'en' ? 'th' : 'en');
    if (act === 'full') toggleFull();
    if (act === 'settings') { if ($('tv-panel').hidden) openPanel(); else $('tv-panel').hidden = true; }
  });
  document.addEventListener('keydown', function (e) {
    if (!$('tv-panel').hidden || /INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) { if (e.keyCode === 27) $('tv-panel').hidden = true; return; }
    var k = e.keyCode;
    if (k === 39) show(index + 1);
    else if (k === 37) show(index - 1);
    else if (k === 32) { e.preventDefault(); $('tv-pause').click(); }
    else if (k === 70) toggleFull();
    else if (k === 76) setLang(cfg.lang === 'en' ? 'th' : 'en');
    else if (k === 83) openPanel();
  });
  ['fullscreenchange', 'webkitfullscreenchange'].forEach(function (e) {
    document.addEventListener(e, function () { $('tv-full').title = isFull() ? t('fullOff') : t('fullOn'); window.setTimeout(redraw, 150); });
  });
  var resizing = null;
  window.addEventListener('resize', function () { window.clearTimeout(resizing); resizing = window.setTimeout(redraw, 200); });

  // ------------------------------------------------------------ นาฬิกา กันจอดับ และเริ่มทำงาน
  function tick() {
    var d = new Date(), p = function (n) { return (n < 10 ? '0' : '') + n; };
    $('tv-clock').textContent = p(d.getDate()) + '/' + p(d.getMonth() + 1) + '/' + d.getFullYear() + '  ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function keepAwake() {
    try { if (navigator.wakeLock && document.visibilityState === 'visible') navigator.wakeLock.request('screen').catch(function () {}); } catch (e) { /* ไม่รองรับ */ }
  }
  document.addEventListener('visibilitychange', keepAwake);

  fit(); chrome(); tick(); keepAwake(); wake();
  stage.innerHTML = '<p class="tv-msg">' + t('loading') + '</p>';
  fetchData(true);
  window.setInterval(tick, 20000);
  window.setInterval(function () { fetchData(false); }, cfg.refresh * 60000);
  // โหลดหน้าใหม่วันละครั้งตอนตีสี่ เพื่อรับโปรแกรมรุ่นใหม่และคืนหน่วยความจำของเบราว์เซอร์
  window.setInterval(function () { var d = new Date(); if (d.getHours() === 4 && d.getMinutes() < 10 && !offline) window.location.reload(); }, 600000);
})();
