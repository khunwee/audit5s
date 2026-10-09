/* 5ส Vision — สคริปต์ฝั่งเบราว์เซอร์ (ไม่พึ่งไลบรารีภายนอก ใช้ในเครือข่ายโรงงานที่ออกเน็ตไม่ได้ก็ทำงาน) */
(function () {
  'use strict';
  var $ = function (s, el) { return (el || document).querySelector(s); };

  // ------------------------------------------------------------ เวลารอคิว: แสดงเป็นนาทีและวินาที แล้วนับถอยหลังเอง
  var dur = function (n) {
    n = Math.max(0, Math.round(n));
    if (n < 60) return n + ' วินาที';
    if (n < 3600) { var s = n % 60; return Math.floor(n / 60) + ' นาที' + (s ? ' ' + s + ' วินาที' : ''); }
    var m = Math.floor(n / 60) % 60;
    return Math.floor(n / 3600) + ' ชั่วโมง' + (m ? ' ' + m + ' นาที' : '');
  };
  // ข้อความรอคิวของภาพหนึ่งใบ: ลำดับ + เวลาที่นับถอยหลัง + เหตุผล (ข้อมูลจาก /api/photos/status)
  var showWait = function (el, prefix, p) {
    if (!el) return;
    while (el.firstChild) el.removeChild(el.firstChild);
    el.appendChild(document.createTextNode(prefix + (p.wait_head || '') + (p.wait_lead ? ' ' + p.wait_lead + ' ' : '')));
    if (p.wait_lead && p.wait_secs !== null && p.wait_secs !== undefined) {
      var b = document.createElement('b');
      b.setAttribute('data-countdown', p.wait_secs);
      b.textContent = dur(p.wait_secs);
      el.appendChild(b);
    }
    if (p.wait_tail) el.appendChild(document.createTextNode(' (' + p.wait_tail + ')'));
  };
  setInterval(function () {
    var list = document.querySelectorAll('[data-countdown]');
    for (var i = 0; i < list.length; i++) {
      var raw = list[i].getAttribute('data-countdown');
      if (raw === '' || raw === null) continue;
      var left = parseInt(raw, 10);
      if (isNaN(left)) continue;
      if (left <= 0) { list[i].textContent = 'อีกสักครู่'; continue; }      // ถึงเวลาที่คาดแล้ว รอข้อมูลรอบถัดไปจากเซิร์ฟเวอร์
      left -= 1;
      list[i].setAttribute('data-countdown', left);
      list[i].textContent = left > 0 ? dur(left) : 'อีกสักครู่';
    }
  }, 1000);
  var $$ = function (s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); };

  // ยืนยันก่อนทำคำสั่งที่ย้อนกลับไม่ได้ และกันกดส่งฟอร์มซ้ำ
  document.addEventListener('submit', function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    var btn = e.target.querySelector && e.target.querySelector('button:not([type=button])');
    if (btn && !e.defaultPrevented) { setTimeout(function () { btn.disabled = true; }, 0); }
  });
  $$('form[data-autosubmit] select').forEach(function (el) {
    el.addEventListener('change', function () { el.form.submit(); });
  });
  document.addEventListener('click', function (e) {
    $$('details.who[open], details.menu[open]').forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute('open'); });
  });

  function fmt(n) { return (Math.round(n * 10) / 10).toString(); }
  function errorText(res, data) {
    if (data && data.detail) return typeof data.detail === 'string' ? data.detail : 'ข้อมูลที่ส่งไม่ครบ';
    if (res.status === 413) return 'ไฟล์ภาพใหญ่เกินไป';
    return 'ทำรายการไม่สำเร็จ (รหัส ' + res.status + ')';
  }

  // ------------------------------------------------------------ หน้าถ่ายและส่งภาพ
  var cap = $('#capture');
  if (cap) {
    var queue = $('#queue'), tpl = $('#card-tpl'), sendbar = $('#sendbar'), sendBtn = $('#send');
    var maxSide = parseInt(cap.getAttribute('data-max-side'), 10) || 1600;
    var afterOf = cap.getAttribute('data-after') || '';
    var scannedArea = cap.getAttribute('data-scanned') || '';
    var items = [], sending = false, pollTimer = null;
    var defined = JSON.parse(cap.getAttribute('data-defined') || '{}');
    var allowFree = cap.getAttribute('data-allow-free') !== '0';

    // จุดตรวจ: แผนกที่โรงงานกำหนดจุดไว้ ให้เลือกจากรายการ (พิมพ์เองได้เมื่อผู้ดูแลอนุญาต)
    var syncArea = function (it) {
      var pick = $('[data-k=area_id]', it.el), chosen = /^[0-9]+$/.test(pick.value);
      var listed = !$('[data-area-pick]', it.el).hidden;
      $('[data-area-name]', it.el).hidden = listed && pick.value !== 'free';
      $('[data-area-type]', it.el).hidden = listed && chosen;
      if (chosen) {
        var type = pick.options[pick.selectedIndex].getAttribute('data-type');
        if (type) $('[data-k=area_type]', it.el).value = type;
      }
    };
    var fillAreas = function (it, keep) {
      var pick = $('[data-k=area_id]', it.el), list = defined[$('#dept').value] || [];
      var old = keep ? pick.value : '';
      while (pick.firstChild) pick.removeChild(pick.firstChild);
      $('[data-area-pick]', it.el).hidden = !list.length;
      if (list.length) {
        var add = function (value, text, type) {
          var o = document.createElement('option');
          o.value = value; o.textContent = text; if (type) o.setAttribute('data-type', type);
          pick.appendChild(o);
        };
        add('', 'เลือกจุดตรวจ');
        list.forEach(function (a) { add(String(a.id), a.name + (a.required ? '' : ' (ไม่บังคับ)'), a.type); });
        if (allowFree) add('free', 'จุดอื่น พิมพ์ชื่อเอง');
        pick.value = old;
        if (pick.selectedIndex < 0) pick.value = '';
      }
      syncArea(it);
    };
    var nameOf = function (it) {
      var pick = $('[data-k=area_id]', it.el);
      if (!$('[data-area-pick]', it.el).hidden) {
        if (/^[0-9]+$/.test(pick.value)) return pick.options[pick.selectedIndex].textContent.replace(' (ไม่บังคับ)', '');
        if (pick.value !== 'free') return '';
      }
      return $('[data-k=area_name]', it.el).value.trim();
    };
    $('#dept').addEventListener('change', function () {
      items.forEach(function (it) { if (it.state === 'new' || it.state === 'fail') fillAreas(it, false); });
    });

    var compress = function (file) {
      // ย่อภาพในเครื่องก่อนส่ง: ประหยัดเน็ตมือถือ และแปลงภาพ HEIC ของ iPhone เป็น JPEG
      return new Promise(function (resolve) {
        var url = URL.createObjectURL(file), img = new Image();
        img.onload = function () {
          try {
            var k = Math.min(1, maxSide / Math.max(img.naturalWidth, img.naturalHeight));
            var c = document.createElement('canvas');
            c.width = Math.round(img.naturalWidth * k); c.height = Math.round(img.naturalHeight * k);
            c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
            c.toBlob(function (blob) { URL.revokeObjectURL(url); resolve(blob || file); }, 'image/jpeg', 0.85);
          } catch (err) { URL.revokeObjectURL(url); resolve(file); }
        };
        img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
        img.src = url;
      });
    };

    // เวลาที่ถ่ายภาพ: อ่านจาก EXIF (DateTimeOriginal) ของไฟล์เดิมก่อนย่อ ถ้าไม่มีใช้เวลาที่แก้ไขไฟล์ล่าสุด
    // ระบบใช้ค่านี้บอกกรรมการว่าภาพจากคลังภาพถ่ายไว้นานแล้วหรือไม่ (ผู้ที่ตั้งใจปลอมเวลาทำได้ จึงเป็นตัวช่วย ไม่ใช่หลักฐาน)
    var pad2 = function (n) { return (n < 10 ? '0' : '') + n; };
    var isoLocal = function (d) {
      return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + 'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
    };
    var exifDate = function (buf) {
      try {
        var v = new DataView(buf), n = v.byteLength, p = 2;
        if (n < 12 || v.getUint16(0) !== 0xFFD8) return '';
        while (p + 4 < n) {
          var marker = v.getUint16(p), size = v.getUint16(p + 2);
          if (marker === 0xFFE1 && v.getUint32(p + 4) === 0x45786966) {            // APP1 "Exif"
            var t = p + 10, le = v.getUint16(t) === 0x4949;
            var u16 = function (o) { return v.getUint16(o, le); }, u32 = function (o) { return v.getUint32(o, le); };
            var find = function (ifd, tag) {
              var count = u16(ifd);
              for (var i = 0; i < count; i++) { var e = ifd + 2 + i * 12; if (e + 12 > n) return 0; if (u16(e) === tag) return e; }
              return 0;
            };
            var text = function (e) {
              if (!e) return '';
              var off = t + u32(e + 8), s = '';
              for (var i = 0; i < 19 && off + i < n; i++) s += String.fromCharCode(v.getUint8(off + i));
              var m = /^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec(s);
              return m ? m[1] + '-' + m[2] + '-' + m[3] + 'T' + m[4] + ':' + m[5] + ':' + m[6] : '';
            };
            var ifd0 = t + u32(t + 4), sub = find(ifd0, 0x8769);
            return (sub ? text(find(t + u32(sub + 8), 0x9003)) : '') || text(find(ifd0, 0x0132));
          }
          if ((marker & 0xFF00) !== 0xFF00 || marker === 0xFFDA) break;
          p += 2 + size;
        }
      } catch (err) { /* ไฟล์ที่อ่านไม่ได้: ถือว่าไม่ทราบเวลา */ }
      return '';
    };
    var shotAt = function (file) {
      return new Promise(function (resolve) {
        var fallback = file.lastModified ? isoLocal(new Date(file.lastModified)) : '';
        if (!file.slice || !window.FileReader) { resolve(fallback); return; }
        var r = new FileReader();
        r.onload = function () { resolve(exifDate(r.result) || fallback); };
        r.onerror = function () { resolve(fallback); };
        r.readAsArrayBuffer(file.slice(0, 262144));
      });
    };

    var refresh = function () {
      var waiting = items.filter(function (i) { return i.state === 'new' || i.state === 'fail'; }).length;
      sendbar.hidden = waiting === 0;
      sendBtn.textContent = sending ? 'กำลังส่ง รออยู่หน้านี้' : 'ส่งภาพ ' + waiting + ' ภาพ';
      sendBtn.disabled = sending;
    };

    var setState = function (it, state, text, photoId) {
      it.state = state;
      it.el.classList.toggle('fail', state === 'fail');
      var box = $('[data-state]', it.el);
      box.textContent = text;
      if (photoId) {
        var a = document.createElement('a');
        a.href = '/photos/' + photoId; a.textContent = 'ดูเหตุผลและคำแนะนำ';
        box.appendChild(document.createTextNode(' ')); box.appendChild(a);
      }
      var rm = $('[data-remove]', it.el);
      if (rm) rm.hidden = !(state === 'new' || state === 'fail');
      refresh();
    };

    var markSent = function (it, title) {
      it.el.classList.add('sent');
      $$('.field', it.el).forEach(function (f) { f.hidden = true; });
      var b = document.createElement('b'); b.textContent = title;
      it.el.children[1].insertBefore(b, it.el.children[1].firstChild);
    };

    var newCard = function () {
      var node = tpl.content.firstElementChild.cloneNode(true);
      var it = { el: node, blob: null, state: 'new', id: null, source: 'mobile', after: '' };
      $('[data-remove]', node).addEventListener('click', function () {
        items.splice(items.indexOf(it), 1); node.remove(); refresh();
      });
      $('[data-k=area_id]', node).addEventListener('change', function () { syncArea(it); });
      fillAreas(it, false);
      return it;
    };

    var addFile = function (file, source) {
      if (!file || (file.type && file.type.indexOf('image/') !== 0)) return;
      var it = newCard(), node = it.el, last = items[items.length - 1];
      it.source = source || 'mobile';
      var typeSel = $('[data-k=area_type]', node);
      if (afterOf && !items.length) {       // ภาพแรกของหน้านี้คือภาพหลังแก้ไขของจุดเดิม
        it.after = afterOf;
        $('[data-k=area_name]', node).value = cap.getAttribute('data-area') || '';
        typeSel.value = cap.getAttribute('data-type') || typeSel.value;
        $('[data-k=note]', node).value = 'ภาพหลังแก้ไขของภาพเลขที่ ' + afterOf;
        var pick = $('[data-k=area_id]', node), want = cap.getAttribute('data-area-id') || '';
        if (!$('[data-area-pick]', node).hidden) {
          pick.value = want; if (pick.selectedIndex < 0 || !want) pick.value = allowFree ? 'free' : '';
          syncArea(it);
        }
      } else if (scannedArea && !$('[data-area-pick]', node).hidden) {    // เปิดจากป้าย QR ของจุดตรวจ
        $('[data-k=area_id]', node).value = scannedArea;
        syncArea(it);
      } else if (last) {                     // จุดตรวจถัดไปมักเป็นพื้นที่ประเภทเดียวกัน
        typeSel.value = $('[data-k=area_type]', last.el).value;
      }
      if (typeSel.selectedIndex < 0) typeSel.selectedIndex = 0;
      queue.appendChild(node);
      items.push(it);
      setState(it, 'new', 'กำลังเตรียมภาพ');
      it.shot = it.source === 'gallery' ? '' : isoLocal(new Date());
      if (it.source === 'gallery') shotAt(file).then(function (v) { it.shot = v; });
      it.ready = compress(file).then(function (blob) {
        it.blob = blob;
        $('img', node).src = URL.createObjectURL(blob);
        setState(it, 'new', 'ยังไม่ได้ส่ง');
        // พาไปช่องชื่อจุดตรวจของภาพแรกที่ยังไม่มีชื่อ แต่ไม่แย่งเคอร์เซอร์ถ้าผู้ใช้กำลังพิมพ์หรือเลือกอยู่ที่ช่องอื่น
        // (รุ่นก่อนหน้านี้ย้ายเคอร์เซอร์ทุกครั้งที่ภาพใบถัดไปเตรียมเสร็จ ตัวอักษรที่กำลังพิมพ์จึงไปลงผิดช่องได้)
        var act = document.activeElement;
        var typing = act && /^(INPUT|SELECT|TEXTAREA)$/.test(act.tagName) && act.type !== 'file';
        var firstBlank = items.filter(function (i) { return i.state === 'new' && !nameOf(i); })[0];
        if (!typing && firstBlank === it) ($('[data-area-pick]', node).hidden ? $('[data-k=area_name]', node) : $('[data-k=area_id]', node)).focus();
      });
    };

    [['cam', 'mobile'], ['gal', 'gallery']].forEach(function (pair) {
      var input = document.getElementById(pair[0]);
      if (!input) return;
      input.addEventListener('change', function () {
        Array.prototype.forEach.call(input.files, function (f) { addFile(f, pair[1]); });
        input.value = '';
      });
    });

    var sendOne = function (it) {
      var name = nameOf(it), pickValue = $('[data-k=area_id]', it.el).value;
      var fd = new FormData();
      if (!$('[data-area-pick]', it.el).hidden && /^[0-9]+$/.test(pickValue)) fd.append('area_id', pickValue);
      fd.append('round_id', $('#round').value);
      fd.append('department_id', $('#dept').value);
      fd.append('area_name', name);
      fd.append('area_type', $('[data-k=area_type]', it.el).value);
      fd.append('note', $('[data-k=note]', it.el).value);
      fd.append('source', it.source);
      if (it.after) fd.append('after_of', it.after);
      if (it.shot) fd.append('shot_at', it.shot);
      fd.append('file', it.blob, 'photo.jpg');
      setState(it, 'sending', 'กำลังส่ง');
      return fetch('/api/photos', { method: 'POST', body: fd, credentials: 'same-origin' }).then(function (res) {
        return res.json().catch(function () { return null; }).then(function (data) {
          if (res.status === 401) { window.location.href = '/login?next=/capture'; return; }
          if (!res.ok) { setState(it, 'fail', errorText(res, data) + ' แก้แล้วกดส่งอีกครั้ง'); return; }
          it.id = data.id;
          markSent(it, name);
          it.stale = !!data.stale;
          setState(it, 'wait', (data.ai_ready ? 'ส่งแล้ว รอ AI วิเคราะห์' : 'ส่งแล้ว จะได้คะแนนเมื่อผู้ดูแลตั้งค่า AI')
            + (it.stale ? ' (ภาพนี้ถ่ายไว้นานแล้ว กรรมการจะเห็นหมายเหตุนี้)' : ''));
        });
      }).catch(function () {
        setState(it, 'fail', 'ส่งไม่สำเร็จ ตรวจสัญญาณอินเทอร์เน็ตแล้วกดส่งอีกครั้ง');
      });
    };

    sendBtn.addEventListener('click', function () {
      if (sending) return;
      if (!$('#dept').value) { window.alert('เลือกแผนกเจ้าของพื้นที่ก่อนส่งภาพ'); $('#dept').focus(); return; }
      var todo = items.filter(function (i) { return i.state === 'new' || i.state === 'fail'; });
      var missing = todo.filter(function (i) { return !nameOf(i); })[0];
      if (missing) {
        window.alert('เลือกหรือใส่ชื่อจุดตรวจให้ครบทุกภาพก่อนส่ง');
        ($('[data-area-pick]', missing.el).hidden ? $('[data-k=area_name]', missing.el) : $('[data-k=area_id]', missing.el)).focus();
        return;
      }
      sending = true; refresh();
      // ภาพที่เพิ่งเลือกอาจยังย่อไม่เสร็จ: รอให้เตรียมครบก่อนแล้วจึงส่งตามลำดับ
      // (รุ่นก่อนหน้านี้ข้ามภาพที่ยังเตรียมไม่เสร็จไปเงียบ ๆ ผู้ใช้กดส่งแล้วเหมือนไม่มีอะไรเกิดขึ้น)
      Promise.all(todo.map(function (i) { return i.ready || Promise.resolve(); })).then(function () {
        var chain = Promise.resolve();
        todo.forEach(function (it) {
          chain = chain.then(function () { return it.blob && items.indexOf(it) >= 0 ? sendOne(it) : null; });
        });
        return chain;
      }).then(function () { sending = false; refresh(); startPoll(); },
              function () { sending = false; refresh(); });
    });

    var poll = function () {
      var wait = items.filter(function (i) { return i.state === 'wait' && i.id; });
      if (!wait.length) { clearInterval(pollTimer); pollTimer = null; return; }
      fetch('/api/photos/status?ids=' + wait.map(function (i) { return i.id; }).join(','), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
          if (!data) return;
          data.photos.forEach(function (p) {
            var it = wait.filter(function (i) { return i.id === p.id; })[0];
            if (!it) return;
            if (p.status === 'done') setState(it, 'done', 'ได้ ' + fmt(p.percent) + '% (' + fmt(p.score) + ' จาก ' + fmt(p.max) + ')', p.id);
            else if (p.status === 'rejected') setState(it, 'done', 'ภาพใช้ประเมินไม่ได้ ถ่ายใหม่', p.id);
            else if (p.status === 'error') setState(it, 'done', 'วิเคราะห์ไม่สำเร็จ ผู้ดูแลจะสั่งวิเคราะห์ใหม่', p.id);
            else if (p.wait) showWait($('[data-state]', it.el), 'ส่งแล้ว ', p);
            else if (data.paused) $('[data-state]', it.el).textContent = 'ส่งแล้ว อยู่ในคิว (' + data.paused + ')';
          });
        }).catch(function () {});
    };
    var startPoll = function () { if (!pollTimer) { pollTimer = setInterval(poll, 4000); poll(); } };

    window.addEventListener('beforeunload', function (e) {
      if (items.some(function (i) { return i.state === 'new' || i.state === 'fail' || i.state === 'sending'; })) {
        e.preventDefault(); e.returnValue = '';
      }
    });

    // ---- เว็บแคม / กล้อง USB (เบราว์เซอร์อนุญาตเฉพาะ https หรือ localhost)
    var wc = $('#webcam'), video = $('#webcam-video'), devSel = $('#webcam-device'), wcErr = $('#webcam-error'), stream = null;
    var wantWidth = parseInt(cap.getAttribute('data-webcam-width'), 10) || 1920;
    var stopCam = function () {
      if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
      video.srcObject = null;
    };
    var camFail = function (text) { wcErr.textContent = text; wcErr.hidden = false; video.hidden = true; };
    var startCam = function (deviceId) {
      stopCam(); wcErr.hidden = true; video.hidden = false;
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        camFail('เบราว์เซอร์เปิดกล้องสดได้เฉพาะเมื่อเข้าระบบผ่าน https หรือ localhost ใช้ปุ่ม ถ่ายภาพ แทนได้'); return;
      }
      var v = { width: { ideal: wantWidth }, height: { ideal: Math.round(wantWidth * 9 / 16) } };
      if (deviceId) v.deviceId = { exact: deviceId }; else v.facingMode = { ideal: 'environment' };
      navigator.mediaDevices.getUserMedia({ video: v, audio: false }).then(function (s) {
        stream = s; video.srcObject = s;
        var active = s.getVideoTracks()[0].getSettings().deviceId;
        return navigator.mediaDevices.enumerateDevices().then(function (list) {
          devSel.innerHTML = '';
          list.filter(function (d) { return d.kind === 'videoinput'; }).forEach(function (d, i) {
            var o = document.createElement('option');
            o.value = d.deviceId; o.textContent = d.label || ('กล้อง ' + (i + 1));
            if (d.deviceId === active) o.selected = true;
            devSel.appendChild(o);
          });
        });
      }).catch(function (err) {
        camFail(err && err.name === 'NotAllowedError' ? 'ยังไม่ได้อนุญาตให้เว็บนี้ใช้กล้อง กดอนุญาตที่แถบที่อยู่ของเบราว์เซอร์แล้วลองใหม่'
          : err && err.name === 'NotFoundError' ? 'ไม่พบกล้องที่ต่ออยู่กับเครื่องนี้' : 'เปิดกล้องไม่ได้ ปิดโปรแกรมอื่นที่ใช้กล้องอยู่แล้วลองใหม่');
      });
    };
    $('#webcam-open').addEventListener('click', function () { wc.hidden = false; startCam(''); wc.scrollIntoView({ block: 'nearest' }); });
    $('#webcam-close').addEventListener('click', function () { stopCam(); wc.hidden = true; });
    devSel.addEventListener('change', function () { startCam(devSel.value); });
    $('#webcam-shot').addEventListener('click', function () {
      if (!stream || !video.videoWidth) return;
      var c = document.createElement('canvas');
      c.width = video.videoWidth; c.height = video.videoHeight;
      c.getContext('2d').drawImage(video, 0, 0);
      c.toBlob(function (blob) { if (blob) addFile(new File([blob], 'webcam.jpg', { type: 'image/jpeg' }), 'webcam'); }, 'image/jpeg', 0.92);
    });
    window.addEventListener('pagehide', stopCam);

    // ---- กล้อง IP: เซิร์ฟเวอร์หรือโปรแกรมในโรงงานเป็นคนถ่าย หน้านี้แค่สั่งและรอผล
    var camOut = $('#cam-out');
    $$('[data-cam]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var label = btn.textContent, fd = new FormData();
        fd.append('round_id', $('#round').value);
        btn.disabled = true; btn.textContent = 'กำลังถ่าย'; camOut.hidden = true;
        fetch('/api/cameras/' + btn.getAttribute('data-cam') + '/capture', { method: 'POST', body: fd, credentials: 'same-origin' })
          .then(function (res) { return res.json().catch(function () { return null; }).then(function (data) {
            camOut.hidden = false;
            if (!res.ok) { camOut.className = 'note red'; camOut.textContent = errorText(res, data); return; }
            if (data.queued) { camOut.className = 'note blue'; camOut.textContent = data.message; return; }
            camOut.hidden = true;
            var it = newCard();
            it.id = data.id; it.source = 'ipcam';
            $('img', it.el).src = '/photos/' + data.id + '/thumb';
            queue.appendChild(it.el); items.push(it);
            markSent(it, data.area);
            setState(it, 'wait', 'ถ่ายจากกล้องแล้ว รอ AI วิเคราะห์');
            startPoll();
          }); })
          .catch(function () { camOut.hidden = false; camOut.className = 'note red'; camOut.textContent = 'ติดต่อเซิร์ฟเวอร์ไม่ได้ ลองอีกครั้ง'; })
          .then(function () { btn.disabled = false; btn.textContent = label; });
      });
    });
  }

  // ------------------------------------------------------------ หน้ารายละเอียดภาพ: รอผลแล้วโหลดใหม่
  var pv = $('[data-poll]');
  if (pv) {
    var t = setInterval(function () {
      fetch('/api/photos/status?ids=' + pv.getAttribute('data-poll'), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
          if (!d || !d.photos[0]) return;
          if (['pending', 'processing'].indexOf(d.photos[0].status) < 0) { clearInterval(t); window.location.reload(); return; }
          var w = $('[data-wait]');
          if (w && d.photos[0].wait) showWait(w, '', d.photos[0]);          // ลำดับในคิวและเวลาที่คาดว่าจะรอ เปลี่ยนตามคิวจริง
        }).catch(function () {});
    }, 5000);
  }

  // ------------------------------------------------------------ หน้ารายการภาพ: มีภาพรอผลอยู่ ได้ผลเมื่อไรโหลดใหม่เอง
  var lp = $('[data-list-poll]');
  if (lp && lp.getAttribute('data-list-poll')) {
    var lt = setInterval(function () {
      if (document.hidden) return;
      fetch('/api/photos/status?ids=' + lp.getAttribute('data-list-poll'), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
          if (!d) return;
          var done = d.photos.filter(function (p) { return ['pending', 'processing'].indexOf(p.status) < 0; });
          if (done.length) { clearInterval(lt); window.location.reload(); return; }
          var cd = $('[data-countdown]', lp), far = null;                  // ปรับเวลาตามภาพที่อยู่ท้ายคิวที่สุด
          d.photos.forEach(function (p) { if (p.wait_secs !== null && (far === null || p.wait_secs > far)) far = p.wait_secs; });
          if (cd && far !== null) { cd.setAttribute('data-countdown', far); cd.textContent = dur(far); }
        }).catch(function () {});
    }, 8000);
  }

  // ------------------------------------------------------------ หน้าคิววิเคราะห์: โหลดใหม่เมื่อคิวเปลี่ยน
  var ql = $('[data-queue-live]');
  if (ql) {
    setInterval(function () {
      if (document.hidden) return;
      fetch('/admin/queue.json', { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
          if (!d) return;
          var sig = d.pending + ':' + d.processing + ':' + d.errors + ':' + (d.paused ? '1' : '0');
          if (sig !== ql.getAttribute('data-queue-live')) window.location.reload();
        }).catch(function () {});
    }, 8000);
  }

  // ------------------------------------------------------------ วาดกรอบโซนบนภาพอ้างอิง (เมาส์และนิ้ว)
  var stage = $('#zone-stage');
  if (stage) {
    var draw = $('#zone-draw'), zform = $('#zone-form'), hint = $('#zone-hint'), start = null;
    var at = function (e) {
      var r = stage.getBoundingClientRect();
      return { x: Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)), y: Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)) };
    };
    var show = function (a, b) {
      var x1 = Math.min(a.x, b.x), y1 = Math.min(a.y, b.y), x2 = Math.max(a.x, b.x), y2 = Math.max(a.y, b.y);
      draw.hidden = false;
      draw.style.left = x1 * 100 + '%'; draw.style.top = y1 * 100 + '%';
      draw.style.width = (x2 - x1) * 100 + '%'; draw.style.height = (y2 - y1) * 100 + '%';
      zform.x1.value = Math.round(x1 * 1000); zform.y1.value = Math.round(y1 * 1000);
      zform.x2.value = Math.round(x2 * 1000); zform.y2.value = Math.round(y2 * 1000);
      hint.textContent = 'กรอบที่วาด: กว้าง ' + Math.round((x2 - x1) * 100) + '% สูง ' + Math.round((y2 - y1) * 100) + '% ของภาพ วาดใหม่ได้ถ้ายังไม่ตรง';
    };
    stage.addEventListener('pointerdown', function (e) { start = at(e); stage.setPointerCapture(e.pointerId); show(start, start); e.preventDefault(); });
    stage.addEventListener('pointermove', function (e) { if (start) show(start, at(e)); });
    stage.addEventListener('pointerup', function (e) { if (start) { show(start, at(e)); start = null; } });
    stage.addEventListener('pointercancel', function () { start = null; });
  }

  // ------------------------------------------------------------ ฟอร์มที่แสดงช่องตามตัวเลือก
  $$('.channel-form').forEach(function (form) {
    var sel = $('[data-kind]', form);
    var show = function () {
      $$('[data-for]', form).forEach(function (box) {
        var on = box.getAttribute('data-for') === sel.value;
        box.hidden = !on;
        $$('input', box).forEach(function (i) { i.disabled = !on; });
      });
    };
    sel.addEventListener('change', show); show();
  });

  // หน้าผู้ใช้: แสดงว่าบัญชีนี้ทำอะไรได้จริง หลังรวมบทบาทกับสิทธิ์ที่ตั้งเฉพาะบัญชี
  var usersBox = $('#users');
  if (usersBox) {
    var rolePerms = JSON.parse(usersBox.getAttribute('data-role-perms') || '{}');
    $$('form', usersBox).forEach(function (form) {
      var role = $('[data-role]', form), box = $('[data-perms]', form);
      if (!role || !box) return;
      var update = function () {
        var admin = role.value === 'admin';
        $('[data-admin-note]', box).hidden = !admin;
        $('[data-perm-rows]', box).hidden = admin;
        $$('[data-perm]', box).forEach(function (sel) {
          var base = (rolePerms[role.value] || []).indexOf(sel.getAttribute('data-perm')) >= 0;
          sel.options[0].textContent = 'ตามบทบาท (' + (base ? 'ได้' : 'ไม่ได้') + ')';
          sel.parentNode.classList.toggle('is-on', sel.value === '1' || (sel.value === '' && base));
        });
      };
      role.addEventListener('change', update);
      $$('[data-perm]', box).forEach(function (sel) { sel.addEventListener('change', update); });
      update();
    });
  }

  // ------------------------------------------------------------ หน้าตั้งค่า AI
  $$('.ai-slot').forEach(function (box) {
    var typeSel = $('[data-f=type]', box), out = $('[data-out]', box);
    var show = function () {
      $$('[data-show]', box).forEach(function (el) {
        el.hidden = el.getAttribute('data-show').split(' ').indexOf(typeSel.value) < 0;
      });
    };
    typeSel.addEventListener('change', show); show();
    var preset = $('[data-preset]', box);
    if (preset) preset.addEventListener('change', function () { if (preset.value) $('[data-f=base]', box).value = preset.value; });
    var payload = function () {
      return { slot: box.getAttribute('data-slot'), type: typeSel.value, base: $('[data-f=base]', box).value,
               key: $('[data-f=key]', box).value, model: $('[data-f=model]', box).value };
    };
    var post = function (url, btn, busy, done) {
      var label = btn.textContent;
      btn.disabled = true; btn.textContent = busy; out.textContent = ''; out.className = '';
      fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify(payload()) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok) { out.className = 'note red'; out.textContent = d.error || d.detail || 'ไม่สำเร็จ'; } else { done(d); }
        })
        .catch(function () { out.className = 'note red'; out.textContent = 'ติดต่อเซิร์ฟเวอร์ไม่ได้ ลองอีกครั้ง'; })
        .then(function () { btn.disabled = false; btn.textContent = label; });
    };
    $('[data-act=models]', box).addEventListener('click', function (e) {
      post('/admin/ai/models', e.target, 'กำลังดึงรายชื่อและตรวจว่ารุ่นใดรับภาพได้', function (d) {
        // รายชื่อเป็นช่องเลือกจริง กดแล้วเห็นครบทุกรุ่น ไม่ขึ้นกับข้อความที่พิมพ์ค้างไว้ในช่องโมเดล
        var pick = $('[data-pick]', box), f = $('[data-f=model]', box), likely = d.likely || [];
        while (pick.firstChild) pick.removeChild(pick.firstChild);
        var add = function (value, text) { var o = document.createElement('option'); o.value = value; o.textContent = text; pick.appendChild(o); };
        add('', 'เลือกโมเดล (' + d.models.length + ' รุ่น)');
        var mark = d.tested ? '  (รับภาพได้ ทดสอบแล้ว)' : '  (น่าจะรับภาพได้)';
        d.models.forEach(function (m) { add(m, m + (d.type !== 'gemini' && likely.indexOf(m) >= 0 ? mark : '')); });
        $('[data-pick-wrap]', box).hidden = !d.models.length;
        pick.value = d.models.indexOf(f.value) >= 0 ? f.value : '';
        out.className = 'note blue';
        if (!d.models.length) {
          out.textContent = 'ไม่พบโมเดลที่ใช้ได้กับ key นี้';
        } else if (!likely.length) {
          out.className = 'note';
          out.textContent = d.tested ? 'พบ ' + d.models.length + ' โมเดล แต่ทดสอบแล้วไม่มีรุ่นใดรับภาพได้ด้วย key นี้ ผู้ให้บริการนี้จึงยังใช้กับระบบไม่ได้ ใช้ผู้ให้บริการอื่นเป็น AI สำรองแทน'
                                   : 'พบ ' + d.models.length + ' โมเดล แต่ไม่มีรุ่นที่ชื่อบ่งว่ารับภาพได้ ดูชื่อรุ่น vision ในเอกสารของผู้ให้บริการ แล้วเลือกจากรายชื่อหรือพิมพ์ชื่อเอง';
        } else {
          out.textContent = 'พบ ' + d.models.length + ' โมเดล เลือกจากรายชื่อด้านบน แล้วกดทดสอบด้วยภาพตัวอย่าง';
          // เติมให้เองเฉพาะเมื่อช่องว่าง หรือชื่อที่ค้างอยู่ไม่มีในรายชื่อของ key นี้
          if (!f.value || d.models.indexOf(f.value) < 0) { f.value = likely[0]; pick.value = likely[0]; }
          else if (likely.indexOf(f.value) < 0) {
            out.className = 'note';
            f.value = likely[0]; pick.value = likely[0];
            out.className = 'note blue';
            out.textContent = 'เปลี่ยนช่องโมเดลเป็น ' + likely[0] + ' ซึ่ง' + (d.tested ? 'ทดสอบแล้วว่ารับภาพได้' : 'น่าจะรับภาพได้') + ' กดทดสอบด้วยภาพตัวอย่างเพื่อยืนยัน';
          }
        }
      });
    });
    $('[data-pick]', box).addEventListener('change', function (e) { if (e.target.value) $('[data-f=model]', box).value = e.target.value; });
    $('[data-act=test]', box).addEventListener('click', function (e) {
      post('/admin/ai/test', e.target, 'กำลังทดสอบ อาจใช้เวลาถึง 1 นาที', function (d) {
        out.className = 'flash'; out.textContent = d.message + ' อย่าลืมกดบันทึกการตั้งค่า';
      });
    });
  });
})();

// กลับมาหน้าเดิมด้วยปุ่มย้อนกลับ: เปิดปุ่มที่ถูกปิดไว้ตอนส่งฟอร์ม
window.addEventListener('pageshow', function (e) {
  if (e.persisted) {
    Array.prototype.forEach.call(document.querySelectorAll('form button[disabled]'), function (b) { b.disabled = false; });
  }
});
