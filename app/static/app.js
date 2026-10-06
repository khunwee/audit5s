/* 5ส Vision — สคริปต์ฝั่งเบราว์เซอร์ (ไม่พึ่งไลบรารีภายนอก ใช้ในเครือข่ายโรงงานที่ออกเน็ตไม่ได้ก็ทำงาน) */
(function () {
  'use strict';
  var $ = function (s, el) { return (el || document).querySelector(s); };
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
      } else if (last) {                     // จุดตรวจถัดไปมักเป็นพื้นที่ประเภทเดียวกัน
        typeSel.value = $('[data-k=area_type]', last.el).value;
      }
      if (typeSel.selectedIndex < 0) typeSel.selectedIndex = 0;
      queue.appendChild(node);
      items.push(it);
      setState(it, 'new', 'กำลังเตรียมภาพ');
      compress(file).then(function (blob) {
        it.blob = blob;
        $('img', node).src = URL.createObjectURL(blob);
        setState(it, 'new', 'ยังไม่ได้ส่ง');
        if (!nameOf(it)) ($('[data-area-pick]', node).hidden ? $('[data-k=area_name]', node) : $('[data-k=area_id]', node)).focus();
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
      fd.append('file', it.blob, 'photo.jpg');
      setState(it, 'sending', 'กำลังส่ง');
      return fetch('/api/photos', { method: 'POST', body: fd, credentials: 'same-origin' }).then(function (res) {
        return res.json().catch(function () { return null; }).then(function (data) {
          if (res.status === 401) { window.location.href = '/login?next=/capture'; return; }
          if (!res.ok) { setState(it, 'fail', errorText(res, data) + ' แก้แล้วกดส่งอีกครั้ง'); return; }
          it.id = data.id;
          markSent(it, name);
          setState(it, 'wait', data.ai_ready ? 'ส่งแล้ว รอ AI วิเคราะห์' : 'ส่งแล้ว จะได้คะแนนเมื่อผู้ดูแลตั้งค่า AI');
        });
      }).catch(function () {
        setState(it, 'fail', 'ส่งไม่สำเร็จ ตรวจสัญญาณอินเทอร์เน็ตแล้วกดส่งอีกครั้ง');
      });
    };

    sendBtn.addEventListener('click', function () {
      if (sending) return;
      if (!$('#dept').value) { window.alert('เลือกแผนกเจ้าของพื้นที่ก่อนส่งภาพ'); $('#dept').focus(); return; }
      var todo = items.filter(function (i) { return (i.state === 'new' || i.state === 'fail') && i.blob; });
      var missing = todo.filter(function (i) { return !nameOf(i); })[0];
      if (missing) {
        window.alert('เลือกหรือใส่ชื่อจุดตรวจให้ครบทุกภาพก่อนส่ง');
        ($('[data-area-pick]', missing.el).hidden ? $('[data-k=area_name]', missing.el) : $('[data-k=area_id]', missing.el)).focus();
        return;
      }
      sending = true; refresh();
      var chain = Promise.resolve();
      todo.forEach(function (it) { chain = chain.then(function () { return sendOne(it); }); });
      chain.then(function () { sending = false; refresh(); startPoll(); });
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
          if (d && d.photos[0] && ['pending', 'processing'].indexOf(d.photos[0].status) < 0) { clearInterval(t); window.location.reload(); }
        }).catch(function () {});
    }, 5000);
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
      post('/admin/ai/models', e.target, 'กำลังดึงรายชื่อ', function (d) {
        var list = $('datalist', box);
        while (list.firstChild) list.removeChild(list.firstChild);
        d.models.forEach(function (m) { var o = document.createElement('option'); o.value = m; list.appendChild(o); });
        out.className = 'note blue';
        out.textContent = d.models.length ? 'พบ ' + d.models.length + ' โมเดล คลิกช่องโมเดลเพื่อเลือก เช่น ' + d.models.slice(0, 4).join(', ')
                                          : 'ไม่พบโมเดลที่ใช้ได้กับ key นี้';
        var f = $('[data-f=model]', box); if (!f.value && d.models.length) f.value = d.models[0];
      });
    });
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
