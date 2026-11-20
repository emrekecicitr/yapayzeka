JS = r'''
(function () {
'use strict';
var TYPE_LABEL = { once: 'Tek seferlik', recurring: 'Tekrarlayan', conditional: 'Koşullu', multistep: 'Çok aşamalı' };
var STATUS_LABEL = { pending: 'Beklemede', scheduled: 'Zamanlandı', running: 'Çalışıyor', paused: 'Duraklatıldı',
  completed: 'Tamamlandı', failed: 'Başarısız', cancelled: 'İptal edildi', awaiting_approval: 'Onay bekliyor' };
var TRIGGER_LABEL = { schedule: 'Zamanlanmış', manual: 'Elle', retry: 'Yeniden deneme', recovery: 'Kurtarma', resume: 'Devam', condition: 'Koşul' };
var RUN_LABEL = { running: 'Çalışıyor', succeeded: 'Başarılı', failed: 'Başarısız', cancelled: 'İptal', paused: 'Duraklatıldı', interrupted: 'Kesintiye uğradı', retry_wait: 'Yeniden deneme bekliyor' };
var DAYS = ['Pzt', 'Sal', 'Çar', 'Per', 'Cum', 'Cmt', 'Paz'];
var S = { tasks: [], stats: null, meta: null, filter: 'all', open: false, pollT: null, sig: '', detail: null, detailT: null,
  notifs: [], unread: 0, notifEnabled: true, F: null, editId: null, badgeT: null, busy: false };

function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ---------- güvenli Markdown (önce tüm HTML kaçırılır; yalnızca http/https bağlantılar) ---------- */
function inl(s) {
  s = esc(s);
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)<>"]+)\)/g, function (m, t, u) {
    return '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + t + '</a>'; });
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<>"')]+)/g, function (m, p, u) {
    return p + '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + u + '</a>'; });
  return s;
}
function mdSafe(src) {
  var lines = String(src == null ? "" : src).replace(/\r\n?/g, '\n').split('\n');
  var out = [], i = 0, para = [], list = null;
  function fp() { if (para.length) { out.push('<p>' + inl(para.join('\n')).replace(/\n/g, '<br>') + '</p>'); para = []; } }
  function fl() { if (list) { out.push('<' + list.t + '>' + list.items.map(function (x) { return '<li>' + inl(x) + '</li>'; }).join("") + '</' + list.t + '>'); list = null; } }
  function cells(l) { return l.trim().replace(/^\|/, "").replace(/\|$/, "").split('|').map(function (c) { return c.trim(); }); }
  while (i < lines.length) {
    var ln = lines[i];
    if (/^```/.test(ln)) {
      fp(); fl(); var code = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i++; }
      i++; out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>'); continue;
    }
    var h = /^(#{1,4})\s+(.*)$/.exec(ln);
    if (h) { fp(); fl(); var lv = Math.min(6, h[1].length + 2); out.push('<h' + lv + '>' + inl(h[2]) + '</h' + lv + '>'); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(ln) && i + 1 < lines.length && /^\s*\|[\s:|\-]+\|\s*$/.test(lines[i + 1])) {
      fp(); fl();
      var head = cells(ln), rows = []; i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(cells(lines[i])); i++; }
      var t = '<div class="tk-tw"><table><thead><tr>' + head.map(function (c) { return '<th>' + inl(c) + '</th>'; }).join("") + '</tr></thead><tbody>';
      rows.forEach(function (r) { t += '<tr>' + r.map(function (c) { return '<td>' + inl(c) + '</td>'; }).join("") + '</tr>'; });
      out.push(t + '</tbody></table></div>'); continue;
    }
    var li = /^\s*([-*]|\d+\.)\s+(.*)$/.exec(ln);
    if (li) {
      fp(); var tt = /\d/.test(li[1]) ? 'ol' : 'ul';
      if (list && list.t !== tt) fl();
      if (!list) list = { t: tt, items: [] };
      list.items.push(li[2]); i++; continue;
    }
    if (/^\s*$/.test(ln)) { fp(); fl(); i++; continue; }
    if (/^-{3,}$/.test(ln.trim())) { fp(); fl(); out.push('<hr>'); i++; continue; }
    fl(); para.push(ln); i++;
  }
  fp(); fl();
  return out.join("");
}

/* ---------- yardımcılar ---------- */
function api(method, url, body) {
  var opt = { method: method, headers: { 'Accept': 'application/json' }, credentials: 'same-origin' };
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  else if (method !== 'GET') { opt.headers['Content-Type'] = 'application/json'; opt.body = '{}'; }
  return fetch(url, opt).then(function (r) {
    return r.json().catch(function () { return null; }).then(function (j) {
      if (!r.ok || !j || j.ok === false) {
        var e = new Error((j && j.error && j.error.message) || ('İstek başarısız (HTTP ' + r.status + ')'));
        e.field = j && j.error && j.error.field; e.status = r.status; throw e;
      }
      return j.data;
    });
  }, function () { var e = new Error('Sunucuya ulaşılamadı. Bağlantınızı kontrol edin.'); e.network = true; throw e; });
}
function toast(msg, isErr) {
  var t = $('tkToast'); if (!t) return;
  t.textContent = msg; t.className = 'tk-toast open' + (isErr ? ' err' : "");
  clearTimeout(toast._t); toast._t = setTimeout(function () { t.className = 'tk-toast'; }, isErr ? 5200 : 2800);
}
function fmtTime(iso, tz) {
  if (!iso) return 'Yok';
  var d = new Date(iso); if (isNaN(d)) return 'Yok';
  var o = { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' };
  try { return d.toLocaleString('tr-TR', Object.assign({ timeZone: tz || 'Europe/Istanbul' }, o)); }
  catch (e) { return d.toLocaleString('tr-TR', o); }
}
function fmtNext(iso, tz) {
  if (!iso) return 'Yok';
  var d = new Date(iso), diff = (d - Date.now()) / 60000;
  if (diff > 0 && diff < 60) return Math.max(1, Math.round(diff)) + ' dk sonra';
  return fmtTime(iso, tz);
}
function dur(a, b) {
  if (!a || !b) return "";
  var s = Math.max(0, Math.round((new Date(b) - new Date(a)) / 1000));
  return s < 60 ? s + ' sn' : Math.floor(s / 60) + ' dk ' + (s % 60) + ' sn';
}
function plain(md) { return String(md || "").replace(/[#*_`>|]+/g, "").replace(/\s+/g, ' ').trim(); }
function snippet(s, n) { s = plain(s); return s.length > n ? s.slice(0, n).trim() + '…' : s; }

/* ---------- liste ---------- */
function btn(act, label, cls, id, extra) {
  return '<button type="button" class="tk-btn sm ' + (cls || "") + '" data-act="' + act + '" data-id="' + id + '"' + (extra || "") + '>' + label + '</button>';
}
function actionsHtml(t, inDetail) {
  var c = t.can || {}, h = "";
  if (!inDetail) h += btn('view', 'Görüntüle', "", t.id);
  if (c.approve) h += btn('approve', 'Onayla ve çalıştır', 'primary', t.id) + btn('reject', 'Reddet', "", t.id);
  if (c.run) h += btn('run', 'Şimdi çalıştır', "", t.id);
  if (c.pause) h += t.pause_requested ? btn('pause', 'Duraklatılıyor…', "", t.id, ' disabled') : btn('pause', 'Duraklat', "", t.id);
  if (c.resume) h += btn('resume', 'Devam ettir', 'primary', t.id);
  if (c.retry) h += btn('retry', 'Tekrar dene', 'primary', t.id);
  if (c.edit) h += btn('edit', 'Düzenle', "", t.id);
  if (t.status === 'running' && c.cancel) h += btn('cancel', t.cancel_requested ? 'İptal ediliyor…' : 'İptal et', "", t.id, t.cancel_requested ? ' disabled' : "");
  if (c.delete) h += btn('delete', 'Sil', 'danger', t.id);
  return h;
}
function cardHtml(t) {
  var running = t.status === 'running';
  var h = '<article class="tk-card' + (running ? ' is-running' : "") + '" data-id="' + t.id + '">';
  h += '<div class="tk-card-top"><h3 class="tk-title" data-act="view" data-id="' + t.id + '">' + esc(t.title) + '</h3>';
  h += '<span class="tk-chip st-' + esc(t.status) + '">' + esc(STATUS_LABEL[t.status] || t.status) + '</span></div>';
  h += '<p class="tk-desc">' + esc(t.description) + '</p>';
  h += '<div class="tk-meta"><span class="tk-chip">' + esc(TYPE_LABEL[t.task_type] || t.task_type) + '</span>';
  h += '<span class="tk-chip">' + (t.is_active ? 'Aktif' : 'Pasif') + '</span>';
  if (t.options && t.options.agent_mode) h += '<span class="tk-chip tk-agent">Ajan</span>';
  if (t.schedule_text) h += '<span>' + esc(t.schedule_text) + '</span>';
  h += '</div>';
  h += '<div class="tk-times"><div><b>Son çalışma</b>' + esc(fmtTime(t.last_run_at, t.timezone)) + '</div><div><b>Sonraki çalışma</b>' + esc(running ? 'Çalışıyor' : fmtNext(t.next_run_at, t.timezone)) + '</div></div>';
  if (running) {
    h += '<div class="tk-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + t.progress + '"><div style="width:' + t.progress + '%"></div></div>';
    h += '<div class="tk-step"><span class="tk-spin"></span> ' + esc(t.current_step || 'Çalışıyor') + ' (%' + t.progress + ')</div>';
  } else if (t.status === 'awaiting_approval' || t.status === 'paused') {
    h += '<div class="tk-step">' + esc(t.current_step || "") + '</div>';
  }
  if (t.last_error && t.last_run_ok === false && !running) h += '<div class="tk-err">' + esc(snippet(t.last_error, 220)) + '</div>';
  if (t.result && !running) h += '<div class="tk-res">' + esc(snippet(t.result, 150)) + '</div>';
  h += '<div class="tk-actions">' + actionsHtml(t, false) + '</div></article>';
  return h;
}
function renderStats() {
  var s = S.stats || {};
  var items = [['Toplam görev', s.total, ""], ['Aktif', s.active, ""], ['Çalışan', s.running, 'run'], ['Tamamlanan', s.completed, 'ok'], ['Başarısız', s.failed, 'bad']];
  $('tkStats').innerHTML = items.map(function (x) { return '<div class="tk-stat ' + x[2] + '"><b>' + (x[1] || 0) + '</b><span>' + x[0] + '</span></div>'; }).join("");
  var b = "";
  if (s.awaiting_approval) b = '<div class="tk-banner"><span>' + s.awaiting_approval + ' görev onayınızı bekliyor.</span><button type="button" class="tk-btn sm primary" id="tkGoApprove">Göster</button></div>';
  $('tkBannerBox').innerHTML = b;
  var nx = s.next_task ? 'Sıradaki görev: ' + esc(snippet(s.next_task.title, 48)) + ', ' + esc(fmtNext(s.next_task.next_run_at, 'Europe/Istanbul')) : (s.running ? "" : 'Zamanlanmış görev yok.');
  $('tkNext').innerHTML = nx;
  var warn = s.scheduler_running === false ? ' Zamanlayıcı çalışmıyor.' : "";
  $('tkSub').textContent = 'Otonom görev merkezi' + warn;
  setBadges(s.unread_notifications || 0);
}
function renderList() {
  var l = $('tkList');
  if (!S.tasks.length) {
    l.innerHTML = '<div class="tk-empty"><b>' + (S.filter === 'all' ? 'Henüz görev yok' : 'Bu filtreye uyan görev yok') + '</b>' +
      (S.filter === 'all' ? 'Yapay zekâya bir görev tanımlayın; planlasın, araştırsın, raporlasın ve size bildirsin.' : "") + '</div>';
    return;
  }
  l.innerHTML = S.tasks.map(cardHtml).join("");
}
function setBadges(n) {
  S.unread = n;
  ['tkSideBadge', 'tkBellBadge'].forEach(function (id) {
    var e = $(id); if (!e) return; e.textContent = n > 99 ? '99+' : String(n); e.hidden = !n;
  });
}
function refresh(force) {
  var my = S.seq = (S.seq || 0) + 1;   // sira numarasi: gec gelen eski yanit yeni listeyi ezmesin
  return Promise.all([api('GET', '/tasks?filter=' + encodeURIComponent(S.filter)), api('GET', '/tasks/stats')]).then(function (r) {
    if (my !== S.seq) return r;
    var sig = JSON.stringify([r[0], r[1]]);
    if (force || sig !== S.sig) { S.sig = sig; S.tasks = r[0]; S.stats = r[1]; renderStats(); renderList(); }
    return r;
  });
}
function schedulePoll() {
  clearTimeout(S.pollT);
  if (!S.open) return;
  var fast = S.stats && S.stats.running > 0;
  S.pollT = setTimeout(function () {
    if (document.hidden) { schedulePoll(); return; }
    refresh(false).catch(function () {}).then(schedulePoll);
  }, fast ? 2500 : 10000);
}

/* ---------- eylemler ---------- */
function confirmBox(msg, yesLabel) {
  return new Promise(function (res) {
    $('tkConfirmMsg').textContent = msg; $('tkConfirmYes').textContent = yesLabel || 'Sil';
    var ov = $('tkConfirmOv');
    function done(v) { ov.classList.remove('open'); $('tkConfirmYes').onclick = null; $('tkConfirmNo').onclick = null; res(v); }
    $('tkConfirmYes').onclick = function () { done(true); }; $('tkConfirmNo').onclick = function () { done(false); };
    ov.classList.add('open'); $('tkConfirmNo').focus();
  });
}
function findTask(id) { for (var i = 0; i < S.tasks.length; i++) if (S.tasks[i].id === id) return S.tasks[i]; return null; }
var MSG = { run: 'Görev kuyruğa alındı.', pause: 'Duraklatma isteği iletildi.', resume: 'Görev devam ettiriliyor.', retry: 'Görev yeniden denenecek.',
  cancel: 'İptal isteği iletildi.', approve: 'Onaylandı, görev çalıştırılacak.', reject: 'Reddedildi.' };
function doAction(act, id) {
  if (act === 'view') return openDetail(id);
  if (act === 'edit') {
    return api('GET', '/tasks/' + id).then(openForm).catch(function (e) { toast(e.message, true); });
  }
  if (act === 'delete') {
    var t = findTask(id) || (S.detail && S.detail.task) || { title: 'Görev' };
    return confirmBox('"' + t.title + '" görevi ve tüm geçmişi kalıcı olarak silinsin mi?', 'Sil').then(function (ok) {
      if (!ok) return;
      return api('DELETE', '/tasks/' + id).then(function () {
        toast('Görev silindi.'); if (S.detail && S.detail.task.id === id) closeOv('tkDetailOv'); return refresh(true);
      }).catch(function (e) { toast(e.message, true); });
    });
  }
  if (act === 'cancel') {
    return confirmBox('Çalışan görev iptal edilsin mi? Kaydedilen adımlar korunmaz.', 'İptal et').then(function (ok) {
      if (!ok) return; return post(act, id);
    });
  }
  return post(act, id);
}
function post(act, id) {
  return api('POST', '/tasks/' + id + '/' + act).then(function () {
    toast(MSG[act] || 'Tamam.'); return refresh(true).then(function () { if (S.detail && S.detail.task.id === id) return loadDetail(id, S.detail.runId); });
  }).catch(function (e) { toast(e.message, true); refresh(true).catch(function () {}); });
}

/* ---------- form ---------- */
function defaultRunAt() {
  var d = new Date(Date.now() + 86400000); d.setHours(9, 0, 0, 0);
  function p(n) { return (n < 10 ? '0' : "") + n; }
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function newF() {
  return { title: "", description: "", type: 'once', mode: 'at', run_at: defaultRunAt(), freq: 'daily', time: '09:00', days: [0], dom: 1,
    interval_min: 60, win_on: false, win_start: '09:00', win_end: '18:00', cron: '0 9 * * 1-5', check_min: 60,
    cond_type: 'news', cond_url: "", cond_keyword: "", cond_op: '>', cond_value: "", cond_query: "", cond_claim: "",
    model: "", internet: true, depth: 'normal', tz: (S.meta && S.meta.default_timezone) || 'Europe/Istanbul',
    fmt: 'rapor', len: 'orta', notify: 'hepsi', approval: false, priority: 2, agent: false, agent_budget: 'normal' };
}
function taskToF(t) {
  var f = newF(), c = t.schedule_config || {}, o = t.options || {};
  f.title = t.title; f.description = t.description; f.type = t.task_type; f.tz = t.timezone;
  f.model = t.selected_model || ""; f.internet = !!t.internet_search_enabled; f.depth = o.research_depth || 'normal';
  f.fmt = o.output_format || 'rapor'; f.len = o.output_length || 'orta'; f.notify = o.notify_pref || 'hepsi';
  f.approval = !!t.approval_required; f.priority = t.task_priority;
  f.agent = !!o.agent_mode; f.agent_budget = o.agent_budget || 'normal';
  if (t.task_type === 'conditional') {
    f.check_min = c.check_interval_minutes || 60;
    var k = o.condition || {}; f.cond_type = k.type || 'news'; f.cond_url = k.url || ""; f.cond_keyword = k.keyword || "";
    f.cond_op = k.operator || '>'; f.cond_value = k.value == null ? "" : String(k.value); f.cond_query = k.query || ""; f.cond_claim = k.claim || "";
  } else {
    if (c.frequency) {
      f.mode = 'recurring'; f.freq = c.frequency; f.time = c.time || f.time; f.days = (c.days_of_week || [0]).slice();
      f.dom = c.day_of_month || 1; f.interval_min = c.interval_minutes || 60; f.cron = c.cron || f.cron;
      if (c.window) { f.win_on = true; f.win_start = c.window.start; f.win_end = c.window.end; }
    } else if (c.when === 'now') f.mode = 'now';
    else if (c.run_at) { f.mode = 'at'; f.run_at = c.run_at; }
    else f.mode = 'manual';
  }
  return f;
}
function selHtml(id, opts, val) {
  return '<select id="' + id + '">' + opts.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (String(o[0]) === String(val) ? ' selected' : "") + '>' + esc(o[1]) + '</option>'; }).join("") + '</select>';
}
function segHtml(key, opts, val) {
  return '<div class="tk-seg" data-seg="' + key + '">' + opts.map(function (o) { return '<button type="button" data-v="' + o[0] + '" class="' + (o[0] === val ? 'on' : "") + '">' + o[1] + '</button>'; }).join("") + '</div>';
}
function fld(id, label, inner, hint) {
  return '<div class="tk-f" id="w_' + id + '"><label for="' + id + '">' + label + '</label>' + inner + (hint ? '<div class="tk-hint">' + hint + '</div>' : "") + '<div class="tk-ferr" id="e_' + id + '"></div></div>';
}
function scheduleHtml() {
  var F = S.F, h = "";
  if (F.type === 'conditional') {
    h += fld('f_check', 'Kontrol sıklığı', selHtml('f_check', [[30, 'Her 30 dakika'], [60, 'Her saat'], [180, 'Her 3 saat'], [360, 'Her 6 saat'], [720, 'Her 12 saat'], [1440, 'Günde bir']], F.check_min), 'Sık kontrol API kredisi tüketir. Kontroller sırasında yalnızca gerekirse model çağrılır.');
    h += fld('f_cond_type', 'Koşul türü', selHtml('f_cond_type', [['news', 'Konuyla ilgili yeni haber çıkınca'], ['web_change', 'Bir web sayfası değişince'], ['threshold', 'Sayfadaki değer eşiği aşınca'], ['search_condition', 'Belirli bir gelişme ortaya çıkınca']], F.cond_type));
    if (F.cond_type === 'web_change' || F.cond_type === 'threshold') h += fld('f_cond_url', 'Web sayfası adresi', '<input type="url" id="f_cond_url" value="' + esc(F.cond_url) + '" placeholder="https://...">');
    if (F.cond_type === 'threshold') {
      h += fld('f_cond_keyword', 'Anahtar kelime', '<input type="text" id="f_cond_keyword" maxlength="100" value="' + esc(F.cond_keyword) + '" placeholder="Örn. Dolar">', 'Değer, sayfada bu kelimeden hemen sonra gelen ilk sayı olarak okunur.');
      h += '<div class="tk-row">' + fld('f_cond_op', 'İşleç', selHtml('f_cond_op', [['>', 'büyüktür'], ['>=', 'büyük eşit'], ['<', 'küçüktür'], ['<=', 'küçük eşit']], F.cond_op)) +
        fld('f_cond_value', 'Eşik değeri', '<input type="number" step="any" id="f_cond_value" value="' + esc(F.cond_value) + '">') + '</div>';
    }
    if (F.cond_type === 'news' || F.cond_type === 'search_condition') h += fld('f_cond_query', 'Arama konusu', '<input type="text" id="f_cond_query" maxlength="200" value="' + esc(F.cond_query) + '" placeholder="Örn. yeni açık kaynak yapay zekâ modeli">');
    if (F.cond_type === 'search_condition') h += fld('f_cond_claim', 'Aranan gelişme', '<input type="text" id="f_cond_claim" maxlength="300" value="' + esc(F.cond_claim) + '" placeholder="Örn. Bir şirket yeni bir model duyurdu">');
    return h;
  }
  var modes = F.type === 'once' ? [['now', 'Hemen'], ['at', 'Belirli zamanda']] : F.type === 'recurring' ? [['recurring', 'Tekrarlayan']] :
    [['manual', 'Elle başlat'], ['now', 'Hemen'], ['at', 'Belirli zamanda'], ['recurring', 'Tekrarlayan']];
  if (modes.length > 1) h += '<div class="tk-f"><span class="tk-lbl">Başlama</span>' + segHtml('mode', modes, F.mode) + '</div>';
  if (F.mode === 'at') h += fld('f_run_at', 'Tarih ve saat', '<input type="datetime-local" id="f_run_at" value="' + esc(F.run_at) + '">', 'Saat, seçili saat dilimine göre yorumlanır (' + esc(F.tz) + ').');
  if (F.mode === 'recurring') {
    h += fld('f_freq', 'Tekrar düzeni', selHtml('f_freq', [['daily', 'Her gün'], ['weekdays', 'Hafta içi'], ['weekly', 'Haftanın seçili günleri'], ['monthly', 'Her ay'], ['interval', 'Belirli aralıkla'], ['cron', 'Özel (cron)']], F.freq));
    if (['daily', 'weekdays', 'weekly', 'monthly'].indexOf(F.freq) >= 0) h += fld('f_time', 'Saat', '<input type="time" id="f_time" value="' + esc(F.time) + '">');
    if (F.freq === 'weekly') h += '<div class="tk-f" id="w_days"><span class="tk-lbl">Günler</span><div class="tk-days" id="f_days">' + DAYS.map(function (d, i) { return '<button type="button" data-day="' + i + '" class="' + (F.days.indexOf(i) >= 0 ? 'on' : "") + '">' + d + '</button>'; }).join("") + '</div><div class="tk-ferr" id="e_days"></div></div>';
    if (F.freq === 'monthly') h += fld('f_dom', 'Ayın günü', '<input type="number" min="1" max="31" id="f_dom" value="' + esc(F.dom) + '">', 'Ay daha kısaysa ayın son günü kullanılır.');
    if (F.freq === 'interval') {
      h += fld('f_interval', 'Aralık (dakika)', '<input type="number" min="15" step="5" id="f_interval" value="' + esc(F.interval_min) + '">', 'En az 15 dakika.');
      h += '<div class="tk-f"><label class="tk-sw"><input type="checkbox" id="f_win_on"' + (F.win_on ? ' checked' : "") + '><span>Yalnızca belirli saat aralığında çalış</span></label></div>';
      if (F.win_on) h += '<div class="tk-row">' + fld('f_win_start', 'Başlangıç', '<input type="time" id="f_win_start" value="' + esc(F.win_start) + '">') + fld('f_win_end', 'Bitiş', '<input type="time" id="f_win_end" value="' + esc(F.win_end) + '">') + '</div>';
    }
    if (F.freq === 'cron') h += fld('f_cron', 'Cron ifadesi', '<input type="text" id="f_cron" maxlength="100" value="' + esc(F.cron) + '">', 'Beş alan: dakika saat gün ay haftanın-günü. Örn. 0 9 * * 1-5 (hafta içi 09:00).');
  }
  if (F.mode === 'manual') h += '<div class="tk-hint" style="margin-bottom:12px">Görev, "Şimdi çalıştır" ile siz başlatana kadar beklemede kalır.</div>';
  return h;
}
function renderSchedule() { var e = $('tkSched'); if (e) e.innerHTML = scheduleHtml(); }
function agentOptsHtml() {
  var b = (S.meta && S.meta.agent && S.meta.agent.budgets) || [{ id: 'dar', turns: 5, searches: 2, reads: 1 }, { id: 'normal', turns: 8, searches: 4, reads: 3 }, { id: 'genis', turns: 12, searches: 6, reads: 5 }];
  var NAMES = { dar: 'Dar', normal: 'Normal', genis: 'Geniş' };
  return fld('f_agent_budget', 'Ajan bütçesi', selHtml('f_agent_budget', b.map(function (x) {
    return [x.id, (NAMES[x.id] || x.id) + ' (en çok ' + x.turns + ' tur, ' + x.searches + ' arama, ' + x.reads + ' sayfa okuma)']; }), S.F.agent_budget),
    'Her tur bir model çağrısıdır. Bütçe, ajanın harcayabileceği API kredisini kesin olarak sınırlar.');
}
function formHtml() {
  var F = S.F, m = (S.meta && S.meta.models) || [], tzs = (S.meta && S.meta.timezones) || ['Europe/Istanbul'];
  if (tzs.indexOf(F.tz) < 0) tzs = tzs.concat([F.tz]);
  var h = fld('f_title', 'Görev adı', '<input type="text" id="f_title" maxlength="120" value="' + esc(F.title) + '" placeholder="Örn. Haftalık yapay zekâ araçları raporu">');
  h += fld('f_description', 'Görev açıklaması', '<textarea id="f_description" maxlength="4000" placeholder="Yapay zekânın ne yapmasını istediğinizi açıkça yazın.">' + esc(F.description) + '</textarea>');
  h += '<div class="tk-f"><span class="tk-lbl">Görev türü</span>' + segHtml('type', [['once', 'Tek seferlik'], ['recurring', 'Tekrarlayan'], ['conditional', 'Koşullu'], ['multistep', 'Çok aşamalı']], F.type) + '<div class="tk-hint" id="tkTypeHint"></div></div>';
  h += '<div id="tkSched">' + scheduleHtml() + '</div>';
  h += fld('f_model', 'Model', selHtml('f_model', [["", 'Varsayılan model']].concat(m.map(function (x) { return [x.id, x.name]; })), F.model), 'Seçilen model yanıt vermezse mevcut yedekleme sırası kullanılır.');
  h += '<div class="tk-f"><label class="tk-sw"><input type="checkbox" id="f_internet"' + (F.internet ? ' checked' : "") + '><span>İnternet araştırması yap</span></label></div>';
  h += '<div id="tkDepth">' + (F.internet ? fld('f_depth', 'Araştırma derinliği', selHtml('f_depth', [['hizli', 'Hızlı (az kaynak)'], ['normal', 'Normal'], ['derin', 'Derin (daha fazla kredi)']], F.depth)) : "") + '</div>';
  h += '<div class="tk-f"><label class="tk-sw"><input type="checkbox" id="f_agent"' + (F.agent ? ' checked' : "") + '><span>Ajan modu: yapay zekâ araçları kendisi seçsin</span></label>' +
    '<div class="tk-hint">Her adımda arama, sayfa okuma, hesaplama veya önceki sonuçları getirme araçlarından birini seçer, sonucu görüp devam eder. Yalnızca okur ve hesaplar, dış sistemde işlem yapmaz.</div></div>';
  h += '<div id="tkAgentOpts">' + (F.agent ? agentOptsHtml() : "") + '</div>';
  h += '<details class="tk-adv"><summary>Gelişmiş ayarlar</summary><div style="padding-bottom:6px">';
  h += fld('f_tz', 'Saat dilimi', selHtml('f_tz', tzs.map(function (z) { return [z, z]; }), F.tz));
  h += '<div class="tk-row">' + fld('f_fmt', 'Çıktı biçimi', selHtml('f_fmt', [['rapor', 'Rapor'], ['ozet', 'Kısa özet'], ['madde', 'Madde listesi'], ['tablo', 'Tablo']], F.fmt)) +
    fld('f_len', 'Çıktı uzunluğu', selHtml('f_len', [['kisa', 'Kısa'], ['orta', 'Orta'], ['uzun', 'Uzun']], F.len)) + '</div>';
  h += '<div class="tk-row">' + fld('f_notify', 'Bildirim', selHtml('f_notify', [['hepsi', 'Tüm sonuçlar'], ['sadece_hatalar', 'Yalnızca hatalar'], ['kapali', 'Kapalı']], F.notify)) +
    fld('f_priority', 'Öncelik', selHtml('f_priority', [[1, 'Yüksek'], [2, 'Normal'], [3, 'Düşük']], F.priority)) + '</div>';
  h += '<div class="tk-f"><label class="tk-sw"><input type="checkbox" id="f_approval"' + (F.approval ? ' checked' : "") + '><span>Her çalıştırmadan önce benden onay iste</span></label></div>';
  h += '</div></details>';
  return h;
}
var TYPE_HINT = { once: 'Belirlenen zamanda bir kez çalışır.', recurring: 'Seçtiğiniz düzende otomatik tekrarlanır.',
  conditional: 'Koşul gerçekleşince çalışır. Koşul düzenli ve ucuz kontrollerle izlenir.', multistep: 'Yapay zekâ görevi küçük adımlara böler; her adımın sonucu kaydedilir.' };
function setTypeHint() { var e = $('tkTypeHint'); if (e) e.textContent = TYPE_HINT[S.F.type] || ""; }
function openForm(task) {
  S.editId = task ? task.id : null;
  S.F = task ? taskToF(task) : newF();
  $('tkFormTitle').textContent = task ? 'Görevi düzenle' : 'Yeni görev';
  $('tkFormBody').innerHTML = formHtml(); $('tkFormErr').textContent = ""; setTypeHint();
  openOv('tkFormOv'); setTimeout(function () { var t = $('f_title'); if (t) t.focus(); }, 30);
}
function readF() {
  var F = S.F;
  function v(id) { var e = $(id); return e ? e.value : null; }
  function c(id) { var e = $(id); return e ? e.checked : false; }
  F.title = v('f_title'); F.description = v('f_description'); F.model = v('f_model'); F.internet = c('f_internet');
  if ($('f_depth')) F.depth = v('f_depth');
  F.agent = c('f_agent'); if ($('f_agent_budget')) F.agent_budget = v('f_agent_budget');
  F.tz = v('f_tz'); F.fmt = v('f_fmt'); F.len = v('f_len'); F.notify = v('f_notify'); F.priority = parseInt(v('f_priority'), 10) || 2; F.approval = c('f_approval');
  if ($('f_run_at')) F.run_at = v('f_run_at');
  if ($('f_freq')) F.freq = v('f_freq');
  if ($('f_time')) F.time = v('f_time');
  if ($('f_dom')) F.dom = parseInt(v('f_dom'), 10);
  if ($('f_interval')) F.interval_min = parseInt(v('f_interval'), 10);
  if ($('f_win_on')) F.win_on = c('f_win_on');
  if ($('f_win_start')) F.win_start = v('f_win_start');
  if ($('f_win_end')) F.win_end = v('f_win_end');
  if ($('f_cron')) F.cron = v('f_cron');
  if ($('f_check')) F.check_min = parseInt(v('f_check'), 10);
  if ($('f_cond_type')) F.cond_type = v('f_cond_type');
  ['url', 'keyword', 'value', 'query', 'claim'].forEach(function (k) { if ($('f_cond_' + k)) F['cond_' + k] = v('f_cond_' + k); });
  if ($('f_cond_op')) F.cond_op = v('f_cond_op');
}
function buildPayload() {
  var F = S.F, p = {
    title: (F.title || "").trim(), description: (F.description || "").trim(), task_type: F.type, timezone: F.tz,
    selected_model: F.model || null, internet_search_enabled: !!F.internet, task_priority: F.priority, approval_required: !!F.approval,
    research_depth: F.depth, output_format: F.fmt, output_length: F.len, notify_pref: F.notify,
    agent_mode: !!F.agent, agent_budget: F.agent_budget
  };
  if (F.type === 'conditional') {
    p.schedule_config = { check_interval_minutes: F.check_min };
    var k = { type: F.cond_type };
    if (F.cond_type === 'web_change' || F.cond_type === 'threshold') k.url = (F.cond_url || "").trim();
    if (F.cond_type === 'threshold') { k.keyword = (F.cond_keyword || "").trim(); k.operator = F.cond_op; k.value = F.cond_value === "" ? null : Number(F.cond_value); }
    if (F.cond_type === 'news' || F.cond_type === 'search_condition') k.query = (F.cond_query || "").trim();
    if (F.cond_type === 'search_condition') k.claim = (F.cond_claim || "").trim();
    p.condition = k; return p;
  }
  var mode = F.type === 'once' ? (F.mode === 'now' ? 'now' : 'at') : F.type === 'recurring' ? 'recurring' : F.mode;
  if (mode === 'manual') p.schedule_config = { manual: true };
  else if (mode === 'now') p.schedule_config = { when: 'now' };
  else if (mode === 'at') p.schedule_config = { run_at: F.run_at };
  else {
    var c = { frequency: F.freq };
    if (['daily', 'weekdays', 'weekly', 'monthly'].indexOf(F.freq) >= 0) c.time = F.time;
    if (F.freq === 'weekly') c.days_of_week = F.days.slice().sort();
    if (F.freq === 'monthly') c.day_of_month = F.dom;
    if (F.freq === 'interval') { c.interval_minutes = F.interval_min; if (F.win_on) c.window = { start: F.win_start, end: F.win_end }; }
    if (F.freq === 'cron') c.cron = F.cron;
    p.schedule_config = c;
  }
  return p;
}
var FIELD_MAP = { title: 'f_title', description: 'f_description', run_at: 'f_run_at', time: 'f_time', day_of_month: 'f_dom', interval_minutes: 'f_interval',
  window: 'f_win_start', cron: 'f_cron', check_interval_minutes: 'f_check', timezone: 'f_tz', selected_model: 'f_model', days_of_week: 'days', condition: 'f_cond_type', agent_mode: 'f_agent', agent_budget: 'f_agent_budget' };
function clearErrs() {
  $('tkFormErr').textContent = "";
  Array.prototype.forEach.call($('tkFormBody').querySelectorAll('.tk-f.bad'), function (e) { e.classList.remove('bad'); });
  Array.prototype.forEach.call($('tkFormBody').querySelectorAll('.tk-ferr'), function (e) { e.textContent = ""; });
}
function showErr(e) {
  var id = FIELD_MAP[e.field], w = id && $('w_' + id);
  if (w) { w.classList.add('bad'); var m = $('e_' + id); if (m) m.textContent = e.message; w.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
  $('tkFormErr').textContent = w ? "" : e.message;
}
function saveForm() {
  if (S.busy) return; readF(); clearErrs();
  var p = buildPayload();
  if (!p.title) return showErr({ field: 'title', message: 'Görev adı gerekli.' });
  if (!p.description) return showErr({ field: 'description', message: 'Görev açıklaması gerekli.' });
  if (p.condition && p.condition.type === 'threshold' && p.condition.value === null) return showErr({ field: 'condition', message: 'Eşik değeri gerekli.' });
  S.busy = true; $('tkFormSave').disabled = true;
  var req = S.editId ? api('PATCH', '/tasks/' + S.editId, p) : api('POST', '/tasks', p);
  req.then(function () {
    closeOv('tkFormOv'); toast(S.editId ? 'Görev güncellendi.' : 'Görev oluşturuldu.'); return refresh(true);
  }).catch(function (e) { showErr(e); }).then(function () { S.busy = false; $('tkFormSave').disabled = false; });
}

/* ---------- ayrıntı ---------- */
var ACT_LABEL = { search: 'Arama', read_page: 'Sayfa okuma', calculate: 'Hesaplama', recall: 'Önceki sonuçlar', finish: 'Bulguları yazma' };
var TURN_STAT = { error: 'Hata', rejected: 'Reddedildi' };
function turnsHtml(turns) {
  return '<ol class="tk-turns">' + turns.map(function (t) {
    var a = t.args || {}, sum = a.query || a.url || a.expression || "";
    var h = '<li class="t-' + esc(t.status) + '"><div class="th"><span class="tn">' + t.turn_no + '</span><b>' + esc(ACT_LABEL[t.action] || t.action) + '</b>' +
      (sum ? '<span class="ta">' + esc(sum) + '</span>' : "") +
      (TURN_STAT[t.status] ? '<span class="tk-chip st-' + (t.status === 'error' ? 'failed' : 'paused') + '">' + TURN_STAT[t.status] + '</span>' : "") + '</div>';
    if (t.thought) h += '<div class="tt">' + esc(t.thought) + '</div>';
    if (t.observation_preview) h += '<details><summary>' + (t.action === 'finish' ? 'Bulgular' : 'Araç sonucu') + '</summary><div class="tobs">' + esc(t.observation_preview) + '</div></details>';
    return h + '</li>';
  }).join("") + '</ol>';
}
var STEP_ICON = { completed: '✓', failed: '✕', running: '◔', pending: '○', skipped: '-' };
var STEP_LABEL = { completed: 'Tamamlandı', failed: 'Başarısız', running: 'Çalışıyor', pending: 'Bekliyor', skipped: 'Atlandı' };
function stepsHtml(steps) {
  if (!steps || !steps.length) return '<div class="tk-hint">Bu görev için henüz adım kaydı yok.</div>';
  return '<ol class="tk-steps">' + steps.map(function (s) {
    var h = '<li class="s-' + s.status + '"><div class="sh"><span class="si">' + (s.status === 'running' ? '<span class="tk-spin"></span>' : STEP_ICON[s.status] || '') + '</span><span style="flex:1;min-width:0">' + esc(s.step_no + '. ' + s.name) + '</span><span class="tk-chip">' + esc(STEP_LABEL[s.status] || s.status) + '</span></div>';
    if (s.error) h += '<div class="se">' + esc(s.error) + '</div>';
    if (s.turns && s.turns.length) h += '<details open><summary>Ajan turları (' + s.turns.length + ')</summary>' + turnsHtml(s.turns) + '</details>';
    if (s.output_preview) h += '<details><summary>Adım çıktısı</summary><div class="tk-md" style="margin-top:6px">' + mdSafe(s.output_preview) + '</div></details>';
    return h + '</li>';
  }).join("") + '</ol>';
}
function loadDetail(id, runId) {
  var reqs = [api('GET', '/tasks/' + id), api('GET', '/tasks/' + id + '/history?limit=20')];
  reqs.push(runId ? api('GET', '/tasks/' + id + '/history/' + runId) : api('GET', '/tasks/' + id + '/steps'));
  return Promise.all(reqs).then(function (r) {
    var task = r[0], runs = r[1], extra = r[2], run = null, steps = [];
    if (runId) { run = extra; steps = extra.steps || []; }
    else { steps = extra.steps || []; }
    var latest = runs.length ? runs[0] : null;
    var needLatest = !runId && latest && latest.has_result;   // kaynak listesi icin guncel calistirmanin ayrintisi
    var next = needLatest ? api('GET', '/tasks/' + id + '/history/' + latest.id) : Promise.resolve(null);
    return next.then(function (ld) {
      S.detail = { task: task, runs: runs, steps: steps, run: run, runId: runId || null, latestRun: ld };
      renderDetail(); scheduleDetailPoll();
    });
  });
}
function renderDetail() {
  var D = S.detail; if (!D) return; var t = D.task;
  $('tkDetailTitle').textContent = t.title;
  var h = '<div class="tk-sec" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center"><span class="tk-chip st-' + esc(t.status) + '">' + esc(STATUS_LABEL[t.status] || t.status) + '</span><span class="tk-chip">' + esc(TYPE_LABEL[t.task_type]) + '</span><span class="tk-chip">' + (t.is_active ? 'Aktif' : 'Pasif') + '</span></div>';
  h += '<div class="tk-sec"><div class="tk-actions">' + actionsHtml(t, true) + '</div></div>';
  h += '<div class="tk-sec"><h4>Görev açıklaması</h4><div class="tk-md" style="white-space:pre-wrap">' + esc(t.description) + '</div></div>';
  h += '<div class="tk-sec"><h4>Zamanlama</h4><dl class="tk-kv"><dt>Düzen</dt><dd>' + esc(t.schedule_text || 'Elle başlatılır') + '</dd><dt>Saat dilimi</dt><dd>' + esc(t.timezone) + '</dd>' +
    '<dt>Sonraki çalışma</dt><dd>' + esc(t.status === 'running' ? 'Çalışıyor' : fmtTime(t.next_run_at, t.timezone)) + '</dd><dt>Son çalışma</dt><dd>' + esc(fmtTime(t.last_run_at, t.timezone)) + '</dd>' +
    '<dt>Çalışma sayısı</dt><dd>' + t.execution_count + (t.retry_count ? ' (yeniden deneme: ' + t.retry_count + ')' : "") + '</dd>' +
    '<dt>Model</dt><dd>' + esc(t.selected_model || 'Varsayılan') + '</dd><dt>İnternet araştırması</dt><dd>' + (t.internet_search_enabled ? 'Açık' : 'Kapalı') + '</dd>' +
    (t.options && t.options.agent_mode ? '<dt>Ajan modu</dt><dd>Açık (bütçe: ' + esc({ dar: 'dar', normal: 'normal', genis: 'geniş' }[t.options.agent_budget] || 'normal') + ')</dd>' : "") +
    (t.approval_required ? '<dt>Onay</dt><dd>Her çalıştırmadan önce istenir</dd>' : "") + '</dl>';
  var cs = t.condition_state || {};
  if (t.task_type === 'conditional' && cs.last_check_note) h += '<div class="tk-hint" style="margin-top:6px">Son koşul kontrolü: ' + esc(fmtTime(cs.last_check_at, t.timezone)) + ', ' + esc(cs.last_check_note) + '</div>';
  h += '</div>';
  if (t.status === 'running') {
    h += '<div class="tk-sec"><h4>Gerçek zamanlı ilerleme</h4><div class="tk-progress"><div style="width:' + t.progress + '%"></div></div><div class="tk-step" style="margin-top:6px"><span class="tk-spin"></span> ' + esc(t.current_step || "") + ' (%' + t.progress + ')</div></div>';
  } else if (t.current_step) h += '<div class="tk-sec"><div class="tk-step">' + esc(t.current_step) + '</div></div>';
  var run = D.run, viewingOld = !!D.runId;
  if (viewingOld) h += '<div class="tk-banner"><span>Geçmiş çalıştırma #' + run.run_no + ' görüntüleniyor.</span><button type="button" class="tk-btn sm" data-act="latest" data-id="' + t.id + '">Güncel sonuca dön</button></div>';
  h += '<div class="tk-sec"><h4>Çalışma planı ve adımlar</h4>' + stepsHtml(D.steps) + '</div>';
  var errTxt = viewingOld ? (run && run.error) : (t.last_run_ok === false ? t.last_error : null);
  if (errTxt) h += '<div class="tk-sec"><h4>Hata kaydı</h4><div class="tk-err">' + esc(errTxt) + '</div></div>';
  var result = viewingOld ? (run && run.result) : (t.result || (D.latestRun && D.latestRun.result));
  var sources = viewingOld ? (run && run.sources) : ((D.latestRun && D.latestRun.sources) || null);
  if (result) {
    h += '<div class="tk-sec"><h4>Sonuç <button type="button" class="tk-btn sm" style="margin-left:8px;text-transform:none;letter-spacing:0" data-act="copy" data-id="' + t.id + '">Kopyala</button></h4><div class="tk-md" id="tkResultMd">' + mdSafe(result) + '</div></div>';
  } else if (t.status !== 'running') h += '<div class="tk-sec"><h4>Sonuç</h4><div class="tk-hint">Henüz sonuç yok.</div></div>';
  if (sources && sources.length) h += '<div class="tk-sec"><h4>Kullanılan kaynaklar</h4><ol class="tk-src">' + sources.map(function (s) {
    var ok = /^https?:\/\//.test(s.url || ""); return '<li>' + (ok ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">' + esc(s.title || s.url) + '</a>' : esc(s.title || "")) + '</li>'; }).join("") + '</ol></div>';
  h += '<div class="tk-sec"><h4>Çalışma geçmişi</h4>' + (D.runs.length ? '<ul class="tk-hist">' + D.runs.map(function (r) {
    return '<li class="' + (D.runId === r.id ? 'sel' : "") + '"><span class="tk-chip st-' + (r.status === 'succeeded' ? 'completed' : r.status === 'failed' ? 'failed' : 'scheduled') + '">' + esc(RUN_LABEL[r.status] || r.status) + '</span>' +
      '<span class="hm">#' + r.run_no + ' · ' + esc(TRIGGER_LABEL[r.trigger] || r.trigger) + ' · ' + esc(fmtTime(r.started_at, t.timezone)) + (r.finished_at ? ' · ' + esc(dur(r.started_at, r.finished_at)) : "") + (r.attempt > 1 ? ' · deneme ' + r.attempt : "") + '</span>' +
      (r.has_result ? '<button type="button" class="tk-btn sm" data-act="showrun" data-id="' + t.id + '" data-run="' + r.id + '">Sonucu göster</button>' : "") +
      (t.can && t.can.run ? '<button type="button" class="tk-btn sm" data-act="run" data-id="' + t.id + '">Yeniden çalıştır</button>' : "") + '</li>'; }).join("") + '</ul>' : '<div class="tk-hint">Henüz çalıştırma yok.</div>') + '</div>';
  var body = $('tkDetailBody'), top = body.scrollTop;
  body.innerHTML = h; body.scrollTop = top;
}
function scheduleDetailPoll() {
  clearTimeout(S.detailT);
  var D = S.detail; if (!D || !$('tkDetailOv').classList.contains('open')) return;
  var live = D.task.status === 'running' || D.task.pause_requested || D.task.cancel_requested;
  if (!live) return;
  S.detailT = setTimeout(function () {
    if (!S.detail || !$('tkDetailOv').classList.contains('open')) return;
    loadDetail(D.task.id, D.runId).catch(function () {}).then(function () { if (S.open) refresh(false).catch(function () {}); });
  }, 2500);
}
function openDetail(id) {
  S.detail = null; $('tkDetailBody').innerHTML = '<div class="tk-hint"><span class="tk-spin"></span> Yükleniyor…</div>'; $('tkDetailTitle').textContent = 'Görev';
  openOv('tkDetailOv');
  return loadDetail(id, null).catch(function (e) { $('tkDetailBody').innerHTML = '<div class="tk-err">' + esc(e.message) + '</div>'; });
}
function copyResult() {
  var D = S.detail; if (!D) return;
  var txt = D.runId && D.run ? D.run.result : (D.task.result || (D.latestRun && D.latestRun.result) || "");
  function fallback() { var ta = document.createElement('textarea'); ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); toast('Sonuç kopyalandı.'); } catch (e) { toast('Kopyalanamadı.', true); } document.body.removeChild(ta); }
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(function () { toast('Sonuç kopyalandı.'); }, fallback); else fallback();
}

/* ---------- bildirimler ---------- */
function renderNotifs() {
  var p = $('tkNotifPanel');
  var h = '<div class="tk-np-h"><span>Bildirimler</span><button type="button" class="tk-btn sm" id="tkNAll">Tümünü okundu yap</button></div>';
  h += '<div style="padding:8px 12px;border-bottom:1px solid var(--hairline-soft)"><label class="tk-sw" style="font-size:13.5px"><input type="checkbox" id="tkNPref"' + (S.notifEnabled ? ' checked' : "") + '><span>Bildirimleri aç (onay istekleri her zaman gelir)</span></label></div>';
  if (!S.notifs.length) h += '<div class="tk-hint" style="padding:16px 14px">Bildirim yok.</div>';
  h += S.notifs.map(function (n) {
    return '<button type="button" class="tk-ni ' + (n.read ? "" : 'unread') + '" data-nid="' + n.id + '" data-task="' + n.task_id + '"><b>' + esc(n.title) + '</b><span>' + esc(n.body) + '</span><i>' + esc(fmtTime(n.created_at)) + '</i></button>';
  }).join("");
  p.innerHTML = h;
}
function loadNotifs() {
  return Promise.all([api('GET', '/tasks/notifications?limit=40'), api('GET', '/tasks/preferences')]).then(function (r) {
    S.notifs = r[0].items; S.notifEnabled = r[1].notifications_enabled; setBadges(r[0].unread); renderNotifs();
  }).catch(function (e) { toast(e.message, true); });
}
function toggleNotifs(force) {
  var p = $('tkNotifPanel'), open = force === undefined ? !p.classList.contains('open') : force;
  p.classList.toggle('open', open); $('tkBell').setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) loadNotifs();
}

/* ---------- pencere yönetimi ---------- */
function openOv(id) { $(id).classList.add('open'); }
function closeOv(id) {
  $(id).classList.remove('open');
  if (id === 'tkDetailOv') { clearTimeout(S.detailT); S.detail = null; }
}
function openPanel() {
  try { if (typeof closeSidebar === 'function') closeSidebar(); } catch (e) {}
  $('tkPanel').classList.add('open'); S.open = true;
  var go = function () { refresh(true).catch(function (e) { toast(e.message, true); }).then(schedulePoll); };
  if (S.meta) go(); else api('GET', '/tasks/meta').then(function (m) { S.meta = m; }).catch(function () {}).then(go);
}
function closePanel() {
  S.open = false; clearTimeout(S.pollT);
  ['tkFormOv', 'tkDetailOv', 'tkConfirmOv'].forEach(function (i) { closeOv(i); });
  toggleNotifs(false); $('tkPanel').classList.remove('open');
}
function pollBadge() {
  clearTimeout(S.badgeT);
  S.badgeT = setTimeout(function () {
    if (!document.hidden && !S.open) api('GET', '/tasks/stats').then(function (s) { setBadges(s.unread_notifications || 0); }).catch(function () {});
    pollBadge();
  }, 30000);
}

/* ---------- olaylar ---------- */
function init() {
  var panel = $('tkPanel'); if (!panel) return;
  $('tkBack').onclick = closePanel;
  $('tkNew').onclick = function () { openForm(null); };
  $('tkBell').onclick = function (e) { e.stopPropagation(); toggleNotifs(); };
  $('tkFormSave').onclick = saveForm;
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !S.open) return;
    var ids = ['tkConfirmOv', 'tkFormOv', 'tkDetailOv'];
    for (var i = 0; i < ids.length; i++) { if ($(ids[i]).classList.contains('open')) { if (ids[i] === 'tkConfirmOv') $('tkConfirmNo').click(); else closeOv(ids[i]); return; } }
    toggleNotifs(false);
  });
  panel.addEventListener('click', function (e) {
    var el = e.target;
    var close = el.closest && el.closest('[data-close]'); if (close) { closeOv(close.getAttribute('data-close')); return; }
    if (el.classList && el.classList.contains('tk-ov') && el.id !== 'tkConfirmOv') { closeOv(el.id); return; }
    var np = $('tkNotifPanel');
    if (np.classList.contains('open') && !el.closest('#tkNotifPanel') && !el.closest('#tkBell')) toggleNotifs(false);
    var f = el.closest && el.closest('[data-filter]');
    if (f) { S.filter = f.getAttribute('data-filter'); Array.prototype.forEach.call($('tkFilters').children, function (c) { c.classList.toggle('on', c === f); }); refresh(true).catch(function (er) { toast(er.message, true); }); return; }
    if (el.id === 'tkGoApprove') { S.filter = 'active'; Array.prototype.forEach.call($('tkFilters').children, function (c) { c.classList.toggle('on', c.getAttribute('data-filter') === 'active'); }); refresh(true); return; }
    var n = el.closest && el.closest('.tk-ni');
    if (n) { var nid = n.getAttribute('data-nid'), tid = parseInt(n.getAttribute('data-task'), 10); api('POST', '/tasks/notifications/' + nid + '/read').then(function (r) { setBadges(r.unread); }).catch(function () {}); toggleNotifs(false); openDetail(tid); return; }
    if (el.id === 'tkNAll') { api('POST', '/tasks/notifications/read-all').then(function () { return loadNotifs(); }).catch(function (er) { toast(er.message, true); }); return; }
    var a = el.closest && el.closest('[data-act]');
    if (a && !a.disabled) {
      var act = a.getAttribute('data-act'), id = parseInt(a.getAttribute('data-id'), 10);
      if (act === 'copy') return copyResult();
      if (act === 'latest') return loadDetail(id, null);
      if (act === 'showrun') return loadDetail(id, parseInt(a.getAttribute('data-run'), 10));
      doAction(act, id);
      return;
    }
    var seg = el.closest && el.closest('[data-seg] button');
    if (seg) {
      var key = seg.parentNode.getAttribute('data-seg'), val = seg.getAttribute('data-v'); readF();
      if (key === 'type') { S.F.type = val; S.F.mode = val === 'once' ? 'at' : val === 'recurring' ? 'recurring' : val === 'multistep' ? 'manual' : S.F.mode; if (val === 'conditional') S.F.internet = true; }
      if (key === 'mode') S.F.mode = val;
      $('tkFormBody').innerHTML = formHtml(); setTypeHint(); return;
    }
    var day = el.closest && el.closest('[data-day]');
    if (day) { var d = parseInt(day.getAttribute('data-day'), 10), ix = S.F.days.indexOf(d); if (ix >= 0) { if (S.F.days.length > 1) S.F.days.splice(ix, 1); } else S.F.days.push(d); day.classList.toggle('on', S.F.days.indexOf(d) >= 0); return; }
  });
  panel.addEventListener('change', function (e) {
    var id = e.target.id;
    if (id === 'tkNPref') { api('POST', '/tasks/preferences', { notifications_enabled: e.target.checked }).then(function (r) { S.notifEnabled = r.notifications_enabled; }).catch(function (er) { toast(er.message, true); }); return; }
    if (!S.F || !$('tkFormOv').classList.contains('open')) return;
    if (id === 'f_freq' || id === 'f_cond_type' || id === 'f_win_on' || id === 'f_tz') { readF(); renderSchedule(); }
    if (id === 'f_agent') { readF(); $('tkAgentOpts').innerHTML = S.F.agent ? agentOptsHtml() : ""; }
    if (id === 'f_internet') { readF(); $('tkDepth').innerHTML = S.F.internet ? fld('f_depth', 'Araştırma derinliği', selHtml('f_depth', [['hizli', 'Hızlı (az kaynak)'], ['normal', 'Normal'], ['derin', 'Derin (daha fazla kredi)']], S.F.depth)) : ""; }
  });
  var sb = $('sidebar');
  if (sb) sb.addEventListener('click', function (e) { if (S.open && !(e.target.closest && e.target.closest('#tkSidebarBtn'))) closePanel(); }, true);
  document.addEventListener('visibilitychange', function () { if (!document.hidden && S.open) refresh(false).catch(function () {}); });
  api('GET', '/tasks/stats').then(function (s) { setBadges(s.unread_notifications || 0); }).catch(function () {});
  pollBadge();
}
window.TasksUI = { open: openPanel, close: closePanel, mdSafe: mdSafe, _state: S };
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { try { init(); } catch (e) { console.error('Görevler arayüzü başlatılamadı', e); } });
else { try { init(); } catch (e) { console.error('Görevler arayüzü başlatılamadı', e); } }
})();
'''
