// 全局 toast 提示
function toast(msg) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.style.display = 'block';
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(function () {
    el.style.display = 'none';
  }, 2200);
}

// 分类搜索栏：下拉选择搜索类型
(function () {
  var typeBtn = document.getElementById('jdb-search-type-btn');
  var menu = document.querySelector('.jdb-search-menu');
  if (!typeBtn || !menu) return;
  var stInput = document.getElementById('jdb-search-st');
  var input = document.getElementById('jdb-search-input');
  var PLACEHOLDER = {
    all: '搜索影片', number: '搜索影片番号', actor: '搜索演员',
    series: '搜索系列', maker: '搜索片商', director: '搜索导演', label: '搜索清单'
  };

  typeBtn.addEventListener('click', function (e) {
    e.stopPropagation();
    menu.classList.toggle('open');
  });
  document.addEventListener('click', function () {
    menu.classList.remove('open');
  });
  menu.addEventListener('click', function (e) {
    e.stopPropagation();
  });

  menu.querySelectorAll('.jdb-search-item').forEach(function (item) {
    item.addEventListener('click', function () {
      var st = item.getAttribute('data-st');
      menu.querySelectorAll('.jdb-search-item').forEach(function (i) { i.classList.remove('active'); });
      item.classList.add('active');
      if (stInput) stInput.value = st;
      var iconSpan = typeBtn.querySelector('.jdb-type-icon');
      var svg = item.querySelector('svg');
      if (iconSpan && svg) iconSpan.innerHTML = svg.outerHTML;
      if (input) input.placeholder = PLACEHOLDER[st] || '搜索';
      menu.classList.remove('open');
      if (input) input.focus();
    });
  });
})();

// 「···」下拉菜单切换
(function () {
  var btn = document.getElementById('nav-more-btn');
  var menu = document.getElementById('nav-menu');
  if (!btn || !menu) return;
  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    menu.classList.toggle('open');
  });
  document.addEventListener('click', function (e) {
    if (!menu.contains(e.target)) menu.classList.remove('open');
  });
  menu.addEventListener('click', function (e) {
    e.stopPropagation();
  });
})();

// 移动端「我的」下拉：横排菜单 + 返回收起
(function () {
  var btn = document.getElementById('mobile-me-btn');
  var sheet = document.getElementById('mobile-me-sheet');
  var back = document.getElementById('mobile-me-back');
  if (!btn || !sheet) return;
  function toggle(open) {
    var on = open === undefined ? !sheet.classList.contains('open') : open;
    sheet.classList.toggle('open', on);
    btn.classList.toggle('active', on);
  }
  btn.addEventListener('click', function (e) { e.stopPropagation(); toggle(); });
  if (back) back.addEventListener('click', function (e) { e.stopPropagation(); toggle(false); });
  document.addEventListener('click', function (e) {
    if (!sheet.contains(e.target) && !btn.contains(e.target)) toggle(false);
  });
  // 移动端眼睛键：切换图片模糊（与桌面 blur-toggle 共用 jd_blur_images）
  var eye = document.getElementById('mobile-me-eye');
  if (eye) {
    var EKEY = 'jd_blur_images';
    function eyeApply() {
      var on = localStorage.getItem(EKEY) === '1';
      document.body.classList.toggle('images-blurred', on);
      eye.classList.toggle('active', on);
      // 「模/清」高亮：模糊时模字高亮，清晰时清字高亮
      var w = eye.querySelector('.mm-eyew'), c = eye.querySelector('.mm-eyec');
      if (w) w.classList.toggle('hl', on);
      if (c) c.classList.toggle('hl', !on);
    }
    eyeApply();
    eye.addEventListener('click', function (e) {
      e.stopPropagation();
      localStorage.setItem(EKEY, localStorage.getItem(EKEY) === '1' ? '0' : '1');
      eyeApply();
    });
  }
})();

// 眼睛：模糊所有图片（睁眼=正常 / 闭眼=模糊 15px）
(function () {
  var btn = document.getElementById('blur-toggle');
  if (!btn) return;
  var KEY = 'jd_blur_images';
  function apply() {
    var on = localStorage.getItem(KEY) === '1';
    document.body.classList.toggle('images-blurred', on);
    btn.classList.toggle('active', on);
  }
  apply();
  btn.addEventListener('click', function () {
    localStorage.setItem(KEY, localStorage.getItem(KEY) === '1' ? '0' : '1');
    apply();
  });
})();

// ===== 全局订阅弹窗（首页/榜单/影库/想看页通用）=====
(function () {
  var form = document.getElementById('want-sub-form');
  if (!form) return;
  var subModal = document.getElementById('want-sub-modal');
  var formError = document.getElementById('want-form-error');
  var currentSubscription = null;
  var pendingButton = null;  // 打开弹窗的来源卡片按钮，保存后立刻标已订阅

  function setModal(open) {
    subModal.hidden = !open;
    if (open) setTimeout(function () { form.elements.target_name.focus(); }, 0);
  }
  document.querySelectorAll('[data-want-modal-close]').forEach(function (b) {
    b.addEventListener('click', function () { setModal(false); });
  });

  function qualityBoxes() { return Array.from(form.querySelectorAll('input[name="qualities"]')); }
  function noneBox() { return form.querySelector('[data-quality-none]'); }
  function isSingleChecked() { return noneBox().checked || qualityBoxes().some(function (b) { return ['hd','uhd'].includes(b.value) && b.checked; }); }
  // 无 / 高清 / 超清 三者只能选一个
  function bindQualitySingle() {
    var singles = [noneBox(),
      form.querySelector('input[name="qualities"][value="hd"]'),
      form.querySelector('input[name="qualities"][value="uhd"]')];
    singles.forEach(function (inp) {
      inp.addEventListener('change', function () {
        if (inp.checked) {
          singles.forEach(function (o) { if (o !== inp) o.checked = false; });
        } else if (!isSingleChecked()) {
          noneBox().checked = true;
        }
      });
    });
    // 字幕 / 破解 可多选，选中时自动取消「无」
    qualityBoxes().forEach(function (b) {
      if (b.value === 'hd' || b.value === 'uhd') return;
      b.addEventListener('change', function () { if (b.checked) noneBox().checked = false; });
    });
  }

  function syncOnlineRow() { form.querySelector('.want-url-row').hidden = form.elements.target_type.value !== 'online'; }
  function categoryBoxes(name) { return Array.from(form.querySelectorAll('input[name="' + name + '"]')); }
  function syncCatLabel(name) {
    var dd = form.querySelector('[data-cat-target="' + name + '"]');
    if (!dd) return;
    var selected = categoryBoxes(name).filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
    var label = dd.querySelector('.cat-dropdown-label');
    label.textContent = selected.length ? selected.slice(0, 3).join('、') + (selected.length > 3 ? ' 等' + selected.length + ' 项' : '') : '选择类别...';
    dd.classList.toggle('has-value', selected.length > 0);
  }
  function closeCatDropdowns() { document.querySelectorAll('.cat-dropdown.open').forEach(function (d) { d.classList.remove('open'); }); }
  function syncActorFields() {
    // 超期设置 + 包含/排除类别：演员订阅、清单订阅才需要（影片订阅不需要）
    var show = form.elements.target_type.value === 'actor' || form.elements.target_type.value === 'list';
    form.querySelectorAll('.want-actor-only').forEach(function (f) { f.hidden = !show; });
    if (!show) ['categories', 'exclude_categories'].forEach(function (n) {
      categoryBoxes(n).forEach(function (b) { b.checked = false; });
      syncCatLabel(n);
    });
  }
  // 影片订阅面向单部影片，不需上映日期筛选；禁用并清空日期。非影片默认今年1月1日~今天。
  function syncDateForType() {
    var movie = form.elements.target_type.value === 'movie';
    var now = new Date(), today = now.toISOString().slice(0, 10), year = now.getFullYear();
    ['release_date_from', 'release_date_to'].forEach(function (k) {
      form.elements[k].disabled = movie;
      if (movie) form.elements[k].value = '';
    });
    if (!movie && !form.elements.release_date_from.value) {
      form.elements.release_date_from.value = year + '-01-01';
      form.elements.release_date_to.value = today;
    }
  }
  form.elements.target_type.addEventListener('change', function () { syncOnlineRow(); syncActorFields(); syncDateForType(); });
  document.querySelectorAll('[data-cat-dropdown]').forEach(function (dd) {
    var btn = dd.querySelector('.cat-dropdown-btn');
    var search = dd.querySelector('.cat-search');
    var list = dd.querySelector('.cat-list');
    btn.addEventListener('click', function (e) {
      e.preventDefault(); e.stopPropagation();
      var wasOpen = dd.classList.contains('open');
      closeCatDropdowns();
      if (!wasOpen) dd.classList.add('open');
      if (search) { search.value = ''; }
      if (list) list.querySelectorAll('.cat-item').forEach(function (i) { i.style.display = ''; });
      if (search && dd.classList.contains('open')) search.focus();
    });
    if (search) search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      list.querySelectorAll('.cat-item').forEach(function (i) {
        var span = i.querySelector('span');
        i.style.display = !q || (span.textContent.toLowerCase().indexOf(q) >= 0) ? '' : 'none';
      });
    });
    dd.querySelectorAll('input[type="checkbox"]').forEach(function (box) {
      box.addEventListener('change', function () { syncCatLabel(box.getAttribute('name')); });
    });
    dd.querySelector('.cat-dropdown-menu').addEventListener('click', function (e) { e.stopPropagation(); });
  });
  document.addEventListener('click', function (e) { if (!e.target.closest('.cat-dropdown')) closeCatDropdowns(); });

  function resetForm() {
    currentSubscription = null;
    pendingButton = null;
    form.reset();
    form.elements.target_type.disabled = false;
    form.elements.id.value = '';
    form.elements.target_id.value = '';
    document.getElementById('want-modal-title').textContent = '创建订阅';
    document.getElementById('want-modal-target').textContent = '设置下载条件';
    formError.hidden = true;
    closeCatDropdowns();
    ['categories', 'exclude_categories'].forEach(function (n) { syncCatLabel(n); });
    syncOnlineRow();
    syncActorFields();
  }
  function openCreate(opts) {
    opts = opts || {};
    resetForm();
    pendingButton = opts.button || null;
    var now = new Date();
    var year = now.getFullYear();
    var today = now.toISOString().slice(0, 10);
    // 默认：预下载 + 高清；（日期仅非影片订阅默认今年1月1日~今天）
    form.elements.pre_download.checked = true;
    var hd = form.querySelector('input[name="qualities"][value="hd"]');
    var uhd = form.querySelector('input[name="qualities"][value="uhd"]');
    if (hd) hd.checked = true;
    if (uhd) uhd.checked = false;
    noneBox().checked = false;
    if (opts.target) {
      form.elements.target_type.value = opts.target.type || 'movie';
      form.elements.target_id.value = opts.target.id || '';
      form.elements.target_name.value = opts.target.name || '';
      if (opts.target.url) form.elements.target_url.value = opts.target.url;
      document.getElementById('want-modal-target').textContent = opts.target.name || '设置下载条件';
    } else {
      form.elements.target_type.value = 'movie';
    }
    syncDateForType();
    // 非影片订阅才给上映日期默认值
    if (form.elements.target_type.value !== 'movie') {
      form.elements.release_date_from.value = opts.dateFrom || (year + '-01-01');
      form.elements.release_date_to.value = opts.dateTo || today;
    }
    syncOnlineRow();
    syncActorFields();
    setModal(true);
  }
  async function openEdit(id, prefill) {
    try {
      var s = prefill || (await fetch('/api/want/subscriptions/' + id).then(function (r) { return r.json(); })).subscription;
      currentSubscription = s;
      form.reset();
      form.elements.id.value = s.id;
      form.elements.target_type.value = s.target_type;
      form.elements.target_type.disabled = true;
      form.elements.target_id.value = s.target_id || '';
      form.elements.target_name.value = s.target_name || '';
      form.elements.target_url.value = s.target_url || '';
      form.elements.download_mode.value = s.download_mode;
      form.elements.pre_download.checked = !!s.pre_download;
      ['min_size_mb','max_size_mb','max_file_count','release_date_from','release_date_to'].forEach(function (key) {
        form.elements[key].value = s[key] == null ? '' : s[key];
      });
      syncDateForType();
      form.elements.expiry_days.value = s.expiry_days == null ? '' : s.expiry_days;
      // 质量单选：无/高清/超清 + 字幕/破解
      noneBox().checked = !(s.qualities || []).length;
      var hd = form.querySelector('input[name="qualities"][value="hd"]');
      var uhd = form.querySelector('input[name="qualities"][value="uhd"]');
      if (hd) hd.checked = (s.qualities || []).indexOf('hd') >= 0;
      if (uhd) uhd.checked = (s.qualities || []).indexOf('uhd') >= 0;
      qualityBoxes().forEach(function (box) {
        if (box.value === 'hd' || box.value === 'uhd') return;
        box.checked = (s.qualities || []).indexOf(box.value) >= 0;
      });
      [['categories', s.categories], ['exclude_categories', s.exclude_categories]].forEach(function (pair) {
        var boxList = categoryBoxes(pair[0]);
        boxList.forEach(function (box) { box.checked = false; });
        (pair[1] || []).forEach(function (cat) {
          var box = boxList.find(function (b) { return b.value === cat; });
          if (box) box.checked = true;
        });
        syncCatLabel(pair[0]);
      });
      syncOnlineRow(); syncActorFields(); formError.hidden = true;
      document.getElementById('want-modal-title').textContent = '编辑订阅';
      document.getElementById('want-modal-target').textContent = s.target_name;
      setModal(true);
    } catch (error) { toast(error.message); }
  }
  function formPayload() {
    var fd = new FormData(form), targetType = form.elements.target_type.value, name = fd.get('target_name').trim();
    return {
      target_type: targetType,
      target_id: fd.get('target_id') || (targetType === 'online' ? '' : name),
      target_name: name,
      target_url: fd.get('target_url').trim(),
      download_mode: fd.get('download_mode'),
      pre_download: form.elements.pre_download.checked,
      qualities: qualityBoxes().filter(function (b) { return b.checked; }).map(function (b) { return b.value; }),
      min_size_mb: fd.get('min_size_mb'), max_size_mb: fd.get('max_size_mb'),
      max_file_count: fd.get('max_file_count'), release_date_from: fd.get('release_date_from'),
      release_date_to: fd.get('release_date_to'), expiry_days: fd.get('expiry_days'),
      categories: categoryBoxes('categories').filter(function (b) { return b.checked; }).map(function (b) { return b.value; }),
      exclude_categories: categoryBoxes('exclude_categories').filter(function (b) { return b.checked; }).map(function (b) { return b.value; })
    };
  }
  async function save() {
    var payload = formPayload();
    if (!payload.target_name) { formError.textContent = '请输入订阅名称'; formError.hidden = false; return; }
    try {
      var result = await fetch(currentSubscription ? '/api/want/subscriptions/' + currentSubscription.id : '/api/want/subscriptions', {
        method: currentSubscription ? 'PATCH' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
      }).then(function (r) { return r.json(); });
      if (!result.ok) throw new Error(result.error || '保存失败');
      return result.subscription.id;
    } catch (error) { formError.textContent = error.message; formError.hidden = false; throw error; }
  }
  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    var after = event.submitter ? event.submitter.value : 'save';
    try {
      var id = await save();
      setModal(false);
      // 保存后：立刻把来源卡片标为已订阅（乐观更新），后台静默跑检查
      var markBtn = pendingButton; pendingButton = null;
      fetch('/api/want/subscriptions/' + id + '/checks', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}).catch(function () {});
      if (markBtn) {
        markBtn.classList.add('subscribed');
        var lbl = markBtn.querySelector('.card-sub-label') || markBtn.querySelector('.actor-sub-label');
        if (lbl) lbl.textContent = '已订阅';
        return;  // 卡片订阅：不提示、不刷新
      }
      toast('订阅已保存');
      window.location.reload();
    } catch (e) { /* 错误已在 save 中展示 */ }
  });
  bindQualitySingle();

  window.SubModal = {
    openCreate: openCreate,
    openEdit: openEdit,
    resetForm: resetForm,
    save: save,
    setModal: setModal
  };
})();

// 想看 / 订阅管理
(function () {
  var root = document.querySelector('[data-want-page]');
  if (!root) return;
  var tabs = Array.from(root.querySelectorAll('[data-want-view]'));
  var list = root.querySelector('[data-want-list]');
  var checkModal = document.getElementById('want-check-modal');
  var checkResults = document.getElementById('want-check-results');

  function escapeHtml(value) {
    var div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  }
  async function api(url, options) {
    var response = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, options || {}));
    var data = await response.json().catch(function () { return {ok:false, error:'服务器返回格式错误'}; });
    if (!response.ok || data.ok === false) throw new Error(data.error || data.message || '操作失败');
    return data;
  }
  function activate(view) {
    if (!tabs.some(function (tab) { return tab.dataset.wantView === view; })) return;
    tabs.forEach(function (tab) {
      var active = tab.dataset.wantView === view;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
    });
    window.location.href = '/want?view=' + encodeURIComponent(view);
  }
  tabs.forEach(function (tab, index) {
    tab.addEventListener('click', function () { activate(tab.dataset.wantView); });
    tab.addEventListener('keydown', function (event) {
      var next = index;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault();
      tabs[next].focus();
      activate(tabs[next].dataset.wantView);
    });
  });

  function setCheckModal(open) { checkModal.hidden = !open; }
  document.querySelectorAll('[data-check-modal-close]').forEach(function (b) { b.addEventListener('click', function () { setCheckModal(false); }); });

  var TAGS = {hd:'高清', uhd:'超清', subtitle:'字幕', edited:'编辑', uncensored:'破解'};
  var REASONS = {quality_not_matched:'质量未命中', below_min_size:'小于最小文件', above_max_size:'大于最大文件', size_unknown:'大小未知', file_count_unknown:'文件数未知', too_many_files:'文件数过多', release_date_unknown:'上映日期未知', release_date_before_start:'早于开始日期', release_date_after_end:'晚于结束日期', category_not_matched:'类别未命中', category_excluded:'类别被排除', blacklisted_movie:'影片在黑名单', blacklisted_actor:'演员在黑名单', blacklisted_list:'清单在黑名单'};
  function renderCandidates(subscriptionId, data) {
    var candidates = data.candidates || [];
    if (!candidates.length) { checkResults.innerHTML = '<div class="empty-card"><p>没有发现磁链资源。</p></div>'; return; }
    checkResults.innerHTML = candidates.map(function (c, index) {
      var allowed = c.push_ok || c.predownload;
      var tags = (c.quality_tags || []).map(function (t) { return '<span>' + (TAGS[t] || t) + '</span>'; }).join('');
      var reasons = (c.rejection_reasons || []).map(function (r) { return REASONS[r] || r; }).join('、');
      return '<article class="want-candidate ' + (index === 0 && allowed ? 'best' : '') + '"><div><h4>' + escapeHtml(c.magnet_name) + '</h4><p>' + escapeHtml(c.size_text || '大小未知') + ' · 评分 ' + c.resource_score.join(' / ') + '</p><div class="want-candidate-tags">' + tags + (c.predownload ? '<span>预下载候选</span>' : '') + '</div>' + (!allowed && reasons ? '<p class="form-error">' + escapeHtml(reasons) + '</p>' : '') + '</div>' + (allowed ? '<button class="glass-button" type="button" data-push-candidate="' + c.id + '" data-subscription="' + subscriptionId + '">推送网盘</button>' : '') + '</article>';
    }).join('');
    checkResults.querySelectorAll('[data-push-candidate]').forEach(function (button) {
      button.addEventListener('click', async function () {
        button.disabled = true;
        try {
          var sid = button.dataset.subscription, cid = button.dataset.pushCandidate;
          var result = await api('/api/want/subscriptions/' + sid + '/candidates/' + cid + '/push', {method:'POST', body:JSON.stringify({idempotency_key:'manual:' + sid + ':' + cid})});
          toast(result.message || '推送成功'); setCheckModal(false); window.location.href = '/want?view=completed';
        } catch (error) { toast(error.message); button.disabled = false; }
      });
    });
  }
  async function runCheck(id) {
    checkResults.innerHTML = '<div class="empty-card"><p>正在检查资源…</p></div>';
    setCheckModal(true);
    try {
      var data = await api('/api/want/subscriptions/' + id + '/checks', {method:'POST', body:'{}'});
      renderCandidates(id, data);
    } catch (error) { checkResults.innerHTML = '<div class="empty-card"><p>' + escapeHtml(error.message) + '</p></div>'; }
  }
  window.runSubscriptionCheck = runCheck;

  // 操作下拉 + 右下角进度面板
  var opsBtn = document.getElementById('want-ops-btn');
  var opsMenu = document.getElementById('want-ops-menu');
  var opPanel = document.getElementById('want-op-panel');
  var opBody = document.getElementById('want-op-panel-body');
  function appendOp(text) {
    if (!opBody) return;
    var line = document.createElement('div');
    line.className = 'op-line';
    line.textContent = text;
    opBody.appendChild(line);
    opBody.scrollTop = opBody.scrollHeight;
  }
  function showOpPanel(show) { if (opPanel) opPanel.hidden = !show; }
  opsBtn.addEventListener('click', function (e) {
    e.stopPropagation();
    opsMenu.style.display = opsMenu.style.display === 'none' ? 'block' : 'none';
  });
  document.addEventListener('click', function (e) {
    if (!e.target.closest('.want-ops')) opsMenu.style.display = 'none';
  });
  opsMenu.addEventListener('click', function (e) { e.stopPropagation(); });
  document.querySelectorAll('#want-ops-menu [data-want-create]').forEach(function (b) {
    b.addEventListener('click', function () { opsMenu.style.display = 'none'; window.SubModal.openCreate(); });
  });
  document.querySelectorAll('#want-ops-menu [data-op="run-all"]').forEach(function (b) {
    b.addEventListener('click', function () { opsMenu.style.display = 'none'; runAllOps(); });
  });
  document.getElementById('want-op-panel-close').addEventListener('click', function () { showOpPanel(false); });

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  async function runAllOps() {
    showOpPanel(true);
    opBody.innerHTML = '';
    appendOp('准备执行…');
    try {
      var data = await api('/api/want/operations/targets');
      var targets = data.targets || [];
      appendOp('共 ' + targets.length + ' 个订阅待执行');
      var matched = 0, rejected = 0, pushed = 0, failed = 0, skipped = 0, confirm = 0;
      for (var i = 0; i < targets.length; i++) {
        var t = targets[i];
        appendOp('[' + (i + 1) + '/' + targets.length + '] 正在检查 ' + t.target_name);
        try {
          var res = await api('/api/want/subscriptions/' + t.id + '/checks', {method:'POST', body:'{}'});
          matched += res.matched_count || 0; rejected += res.rejected_count || 0;
          appendOp('   完成：命中 ' + (res.matched_count || 0) + '、拒绝 ' + (res.rejected_count || 0));
          if (res.matched_count < 1) { appendOp('   无命中候选，跳过推送'); continue; }
          if (t.pre_download) { confirm++; appendOp('   预下载：有待确认的候选'); continue; }
          // 提交到 115 后由后台验证并自动换磁链重试，不立即判成功
          var pr = await api('/api/want/subscriptions/' + t.id + '/auto-push', {method:'POST', body:'{}'});
          if (pr.ok) { pushed++; appendOp('   已提交：' + (pr.message || '')); }
          else if (/入库|跳过/.test(pr.message || '')) { skipped++; appendOp('   跳过：' + pr.message); }
          else { failed++; appendOp('   提交失败：' + pr.message); }
          await sleep(3000);
        } catch (err) { failed++; appendOp('   失败：' + err.message); }
      }
      appendOp('全部完成：命中 ' + matched + '、拒绝 ' + rejected + '、已推送 ' + pushed + '、跳过 ' + skipped + '、失败 ' + failed + (confirm ? '、待确认 ' + confirm : ''));
    } catch (err) { appendOp('出错：' + err.message); }
  }

  // 演员影片弹窗（参考 8.png）
  var actorModal = document.getElementById('want-actor-movies-modal');
  var actorGrid = document.getElementById('want-actor-movies-grid');
  var actorTabs = document.getElementById('want-actor-tabs');
  var actorOpMenu = document.getElementById('want-actor-op-menu');
  var actorModalSub = document.getElementById('want-actor-movies-sub');
  var currentActor = null;
  var CLOCK_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>';
  var TAB_ORDER = ['active', 'completed', 'skipped', 'all'];
  var TAB_TEXT = {active:'订阅中', completed:'已完成', skipped:'跳过', all:'全部'};
  var STATUS_TEXT = {active:'订阅中', completed:'已完成', skipped:'跳过'};

  function setActorModal(open) { actorModal.hidden = !open; if (!open) actorOpMenu.style.display = 'none'; }
  document.querySelectorAll('[data-actor-movies-close]').forEach(function (b) {
    b.addEventListener('click', function () { setActorModal(false); });
  });

  var currentActorFilter = 'active';
  async function loadActorMovies() {
    if (!currentActor) return;
    var filter = currentActorFilter;
    actorGrid.innerHTML = '<div class="empty-card" style="grid-column:1/-1;"><p>加载中…</p></div>';
    try {
      var data = await api('/api/want/subscriptions/' + currentActor.id + '/movies?filter=' + encodeURIComponent(filter));
      renderActorTabs(data.counts, filter);
      if (data.name) document.getElementById('want-actor-movies-title').textContent = data.name + ' 的影片';
      if (data.checked_at) actorModalSub.textContent = '最近检查 ' + data.checked_at;
      renderActorMovies(data.movies, data.checked_at, data.filter);
    } catch (error) { actorGrid.innerHTML = '<div class="empty-card" style="grid-column:1/-1;"><p>' + escapeHtml(error.message) + '</p></div>'; }
  }
  function renderActorTabs(counts, filter) {
    actorTabs.innerHTML = TAB_ORDER.map(function (tab) {
      var active = tab === filter ? ' active' : '';
      return '<button type="button" class="rank-tab' + active + '" data-tab="' + tab + '">' + TAB_TEXT[tab] + '<span class="subtle-count">' + (counts[tab] || 0) + '</span></button>';
    }).join('');
    actorTabs.querySelectorAll('.rank-tab').forEach(function (btn) {
      btn.addEventListener('click', function () { currentActorFilter = btn.dataset.tab; loadActorMovies(); });
    });
  }
  function renderActorMovies(movies, checkedAt, filter) {
    if (!movies.length) { actorGrid.innerHTML = '<div class="empty-card" style="grid-column:1/-1;"><p>暂无影片。</p></div>'; return; }
    actorGrid.innerHTML = movies.map(function (m) {
      var cover = m.cover ? imgproxy(m.cover) : '';
      var img = cover ? '<img src="' + cover + '" loading="lazy" alt="' + escapeHtml(m.number) + '">' : '<div class="placeholder">无封面</div>';
      var statusCls = m.sub_status === 'completed' ? 'done' : m.sub_status === 'skipped' ? 'skip' : 'active';
      var skipBtn = m.sub_status === 'skipped'
        ? '<button type="button" class="smc-act" data-unskip="' + m.id + '" title="恢复">恢复</button>'
        : '<button type="button" class="smc-act" data-skip="' + m.id + '" title="跳过">跳过</button>';
      var libFlag = m.in_library
        ? '<span class="smc-lib">已入库</span>'
        : '<span class="smc-lib missing">未入库</span>';
      return '<a class="sub-movie-card" href="/movie/' + encodeURIComponent(m.id) + '"><div class="smc-cover">' + img +
        '<span class="smc-status ' + statusCls + '">' + (STATUS_TEXT[m.sub_status] || '订阅中') + '</span>' +
        libFlag +
        '<div class="smc-cover-actions">' + skipBtn + '</div></div>' +
        '<div class="smc-info"><div class="smc-meta"><span class="num-tag">' + escapeHtml(m.number) + '</span>' +
        (m.release_date ? '<span class="smc-date">' + escapeHtml(m.release_date) + '</span>' : '') + '</div>' +
        '<div class="smc-title">' + escapeHtml(m.title) + '</div>' +
        '<div class="smc-foot">' + CLOCK_SVG + ' ' + escapeHtml(checkedAt || '') + '</div></div></a>';
    }).join('');
    actorGrid.querySelectorAll('[data-skip]').forEach(function (btn) {
      btn.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); setMovieSkip(btn.dataset.skip, true); });
    });
    actorGrid.querySelectorAll('[data-unskip]').forEach(function (btn) {
      btn.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); setMovieSkip(btn.dataset.unskip, false); });
    });
  }
  async function setMovieSkip(movieId, skip) {
    try {
      await api('/api/want/subscriptions/' + currentActor.id + '/movies/' + movieId + (skip ? '/skip' : '/unskip'), {method:'POST', body:'{}'});
      loadActorMovies();
    } catch (error) { toast(error.message); }
  }
  // 点击演员/清单卡（头像/字母徽标）打开影片弹窗
  list.addEventListener('click', function (event) {
    var card = event.target.closest('[data-actor-subscription]') || event.target.closest('[data-movies-subscription]');
    if (card && !event.target.closest('button')) {
      currentActor = {id: card.dataset.actorSubscription || card.dataset.moviesSubscription, name: card.dataset.actorName || card.dataset.moviesName};
      setActorModal(true); loadActorMovies();
      return;
    }
    // 影片订阅卡：点击非按钮区域跳转到详情页
    var sub = event.target.closest('[data-movie-url]');
    if (sub && !event.target.closest('button')) {
      event.preventDefault();
      window.location.href = sub.dataset.movieUrl;
    }
  });
  // 操作下拉
  document.getElementById('want-actor-op-btn').addEventListener('click', function (e) {
    e.stopPropagation();
    actorOpMenu.style.display = actorOpMenu.style.display === 'none' ? 'block' : 'none';
  });
  document.addEventListener('click', function (e) { if (!e.target.closest('.want-actor-op')) actorOpMenu.style.display = 'none'; });
  document.getElementById('want-actor-op-menu').addEventListener('click', function (e) {
    var op = e.target.dataset.actorOp;
    if (!op || !currentActor) return;
    actorOpMenu.style.display = 'none';
    if (op === 'check') {
      api('/api/want/subscriptions/' + currentActor.id + '/checks', {method:'POST', body:'{}'})
        .then(function () { toast('已发起检查'); loadActorMovies(); })
        .catch(function (err) { toast(err.message); });
    } else if (op === 'subscribe') {
      // 执行订阅：逐个推送本窗口“订阅中”影片，右下角进度窗显示 N/总数
      var subId = currentActor.id;
      showOpPanel(true); opBody.innerHTML = '';
      appendOp('正在执行订阅…');
      api('/api/want/subscriptions/' + subId + '/checks', {method:'POST', body:'{}'}).catch(function () {});
      api('/api/want/subscriptions/' + subId + '/movies?filter=active')
        .then(function (d) {
          var movies = d.movies || [];
          var total = movies.length;
          if (!total) { appendOp('没有订阅中的影片'); setTimeout(function(){ showOpPanel(false); }, 3000); return; }
          appendOp('订阅中影片 ' + total + ' 部');
          var pushed = 0, skipped = 0, failed = 0;
          function next(i) {
            if (i >= total) {
              appendOp('已推送成功 ' + pushed + ' 部' + (skipped ? '，已跳过 ' + skipped : '') + (failed ? '，失败 ' + failed : ''));
              setTimeout(function () { showOpPanel(false); }, 3000);
              loadActorMovies();
              return;
            }
            fetch('/api/want/subscriptions/' + subId + '/movies/' + movies[i].id + '/subscribe', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
              .then(function (r) { return r.json(); })
              .then(function (r) {
                if (r && r.ok !== false) pushed++;
                else if (/磁链|已入库|跳过/.test(r.message || '')) skipped++;
                else failed++;
                appendOp('已推送 ' + pushed + '/' + total);
              })
              .catch(function () { failed++; appendOp('已推送 ' + pushed + '/' + total); })
              .then(function () { next(i + 1); });
          }
          next(0);
        })
        .catch(function (err) { appendOp('执行失败：' + err.message); setTimeout(function(){ showOpPanel(false); }, 3000); });
    }
  });

  function imgproxy(u) { return u ? '/img?url=' + encodeURIComponent(u) + '&v=3' : ''; }

  // 卡片检查按钮：图标转圈，不弹窗，完成后刷新
  async function silentCheck(button) {
    if (button.disabled) return;
    var id = button.dataset.subCheck;
    var original = button.innerHTML;
    button.disabled = true;
    button.classList.add('checking');
    button.innerHTML = '<span class="btn-spinner"></span>';
    try {
      await api('/api/want/subscriptions/' + id + '/checks', {method:'POST', body:'{}'});
      toast('检查完成');
      window.location.reload();
    } catch (e) {
      toast(e.message);
      button.disabled = false;
      button.classList.remove('checking');
      button.innerHTML = original;
    }
  }

  list.addEventListener('click', async function (event) {
    var button = event.target.closest('button'); if (!button) return;
    if (button.dataset.subEdit) return window.SubModal.openEdit(button.dataset.subEdit);
    if (button.dataset.subCheck) return silentCheck(button);
    try {
      if (button.dataset.subDelete) {
        if (!confirm('确定删除这条订阅？')) return;
        await api('/api/want/subscriptions/' + button.dataset.subDelete, {method:'DELETE'}); window.location.reload();
      } else if (button.dataset.subAction) {
        await api('/api/want/subscriptions/' + button.dataset.id + '/' + button.dataset.subAction, {method:'POST', body:'{}'}); window.location.reload();
      } else if (button.dataset.blacklistDelete) {
        await api('/api/want/blacklist/' + button.dataset.blacklistDelete, {method:'DELETE'}); window.location.reload();
      }
    } catch (error) { toast(error.message); }
  });
})();

// 通用订阅入口（影片/演员/清单）：显示已订阅状态；点击先弹订阅弹窗（未订阅=创建带默认值，已订阅=编辑）。
(function () {
  var buttons = Array.from(document.querySelectorAll(
    '.card-sub[data-subscribe-target][data-target-type="movie"], ' +
    'button[data-subscribe-target][data-target-type="movie"], ' +
    'button[data-subscribe-target][data-target-type="list"], ' +
    '.actor-sub-btn[data-subscribe-actor]'
  ));
  if (!buttons.length) return;

  function typeOf(b) { return b.getAttribute('data-target-type') || (b.hasAttribute('data-subscribe-actor') ? 'actor' : 'movie'); }
  function idOf(b) { return b.dataset.targetId || b.dataset.actorId; }
  function nameOf(b) { return b.dataset.targetName || b.dataset.actorName || idOf(b); }
  function mark(button, subscribed) {
    button.classList.toggle('subscribed', subscribed);
    var label = button.querySelector('.card-sub-label') || button.querySelector('.actor-sub-label');
    if (label) label.textContent = subscribed ? '已订阅' : '订阅';
  }
  function openCreateFor(button) {
    window.SubModal.openCreate({target:{type:typeOf(button), id:idOf(button), name:nameOf(button)}, button: button});
  }
  function unSubscribe(button) {
    mark(button, false);
    fetch('/api/want/subscriptions/unsubscribe', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({target_type:typeOf(button), target_id:idOf(button)})
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.ok) { toast('已取消订阅'); }
      else { mark(button, true); toast(d.error || '取消失败'); }
    }).catch(function () { mark(button, true); toast('取消失败'); });
  }

  // 页面加载时按类别批量查询已订阅状态
  var byType = {};
  buttons.forEach(function (b) {
    (byType[typeOf(b)] = byType[typeOf(b)] || []).push(idOf(b));
  });
  Object.keys(byType).forEach(function (type) {
    var ids = byType[type].filter(Boolean);
    if (!ids.length) return;
    fetch('/api/want/subscribe-state', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({type:type, ids:ids})
    }).then(function (r){return r.json();}).then(function (d) {
      var set = new Set(d.subscribed || []);
      buttons.forEach(function (b) { if (typeOf(b) === type) mark(b, set.has(idOf(b))); });
    }).catch(function () {});
  });

  buttons.forEach(function (button) {
    button.addEventListener('click', function (event) {
      event.preventDefault(); event.stopPropagation();
      if (button.classList.contains('subscribed')) { unSubscribe(button); return; }  // 已订阅→取消
      openCreateFor(button);  // 未订阅→弹订阅弹窗
    });
  });
})();

