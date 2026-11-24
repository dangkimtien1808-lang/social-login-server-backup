// Accounts Manager（唯讀）：只呼叫 GET，不儲存任何資料。
(function(){
  'use strict';

  let allAccounts = [];
  const $ = function(id){ return document.getElementById(id); };

  // 一律使用 textContent 建立元素，避免資料被當成 HTML 執行
  function el(tag, cls, text){
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function badge(status){
    const s = String(status || 'unknown').toLowerCase();
    let cls = 'b-gray';

    if (s === 'ready' || s === 'success') cls = 'b-green';
    else if (s === 'running' || s === 'queued' || s === 'pending') cls = 'b-amber';
    else if (s === 'failed' || s === 'locked' || s === 'error') cls = 'b-red';
    else if (s === 'need_verification') cls = 'b-purple';

    return el('span', 'badge ' + cls, s);
  }

  function chip(label, yes){
    return el('span', 'am-chip ' + (yes ? 'yes' : 'no'), (yes ? '✓ ' : '✗ ') + label);
  }

  function formatTime(iso){
    return new Date(iso).toLocaleString('zh-TW', {timeZone:'Asia/Taipei', hour12:false});
  }

  function notice(msg, kind){
    $('notice').className = kind || '';
    $('notice').textContent = msg || '';
  }

  function cell(){
    const td = el('td');
    for (const child of arguments) if (child) td.appendChild(child);
    return td;
  }

  function buildRow(a){
    const tr = el('tr');

    const creds = el('div', 'am-chips');
    creds.append(chip('email', a.has_email), chip('password', a.has_password),
      chip('cookie', a.has_cookie), chip('token', a.has_token), chip('cred', a.has_cred));

    const profile = el('div');
    profile.append(chip('profile', a.profile_dir_exists),
      el('div', 'small mono', a.profile_dir_display));
    const session = el('div');
    session.append(chip('session', a.session_file_exists),
      el('div', 'small mono', a.session_file_display));

    const warnings = (a.warnings || []).map(function(w){
      return el('div', 'am-warn', '⚠ ' + w);
    });

    tr.append(
      cell(el('div', 'name', a.name), el('div', 'small', a.platform)),
      cell(badge(a.status_current_dashboard)),
      cell(el('span', 'mono', a.email_masked || a.uid_masked || '-')),
      cell(creds),
      cell(profile, session),
      cell(
        a.last_job_status ? badge(a.last_job_status) : el('span', 'small', '-'),
        a.last_job_time ? el('div', 'small', formatTime(a.last_job_time)) : null,
        a.last_job_error_summary ? el('div', 'am-error', a.last_job_error_summary) : null
      ),
      cell(el('span', 'small', a.schedule_group || '-')),
      cell.apply(null, warnings.length ? warnings : [el('span', 'small', '-')])
    );
    return tr;
  }

  function messageRow(text){
    const td = el('td', 'small', text);
    td.colSpan = 8;
    const tr = el('tr');
    tr.appendChild(td);
    return tr;
  }

  function render(){
    const platform = $('platformFilter').value;
    const list = allAccounts.filter(function(a){
      return !platform || a.platform === platform;
    });

    $('amCount').textContent = list.length + ' / ' + allAccounts.length + ' accounts';
    $('amRows').replaceChildren.apply($('amRows'),
      list.length ? list.map(buildRow) : [messageRow('沒有任何帳號。')]);
  }

  async function load(){
    $('refreshBtn').disabled = true;

    try {
      const res = await fetch('/accounts/manage', {cache:'no-store'});
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();

      allAccounts = data.accounts || [];
      render();
      $('apiBadge').textContent = 'API: online';
      $('apiBadge').className = 'badge b-green';

      const errors = data.errors || [];
      notice(errors.map(function(e){
        return '無法讀取 ' + e.platform + '：' + e.error;
      }).join(' / '), errors.length ? 'err' : '');
    } catch (e) {
      $('apiBadge').textContent = 'API: error';
      $('apiBadge').className = 'badge b-red';
      notice('載入 /accounts/manage 時發生錯誤：' + e.message, 'err');
      if (!allAccounts.length) $('amRows').replaceChildren(messageRow('載入失敗。'));
    } finally {
      $('refreshBtn').disabled = false;
    }
  }

  function clearForm(){
    $('addPlatform').selectedIndex = 0;
    for (const id of ['addName', 'addEmail', 'addPassword', 'addCookie', 'addToken']){
      $(id).value = '';
    }
  }

  $('refreshBtn').addEventListener('click', load);
  $('platformFilter').addEventListener('change', render);
  $('addBtn').addEventListener('click', function(){ $('addDialog').showModal(); });
  $('addCancel').addEventListener('click', function(){ $('addDialog').close(); });
  // 關閉（含 Esc）時清空輸入內容
  $('addDialog').addEventListener('close', clearForm);

  load();
})();
