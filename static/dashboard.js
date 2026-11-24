let selectedJobId = null;
let latestAccounts = [];
let latestJobs = [];

function esc(s){
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}

function jsArg(s){
  return String(s == null ? '' : s)
    .replace(/\\/g,'\\\\')
    .replace(/'/g,"\\'")
    .replace(/\n/g,'\\n')
    .replace(/\r/g,'');
}

function shortId(id){
  return id ? String(id).slice(0,8) : '-';
}

function statusBadge(status){
  const s = String(status || 'unknown').toLowerCase();
  let cls = 'b-gray';

  if (s === 'ready' || s === 'success') cls = 'b-green';
  else if (s === 'running' || s === 'queued') cls = 'b-amber';
  else if (s === 'failed' || s === 'locked' || s === 'error') cls = 'b-red';
  else if (s === 'need_verification') cls = 'b-purple';

  return '<span class="badge ' + cls + '">' + esc(s) + '</span>';
}

function platformIcon(platform){
  const p = String(platform || '').toLowerCase();
  if (p === 'facebook') return '📘';
  if (p === 'tiktok') return '🎵';
  return '🌐';
}

function notice(msg, kind){
  const el = document.getElementById('notice');
  el.className = kind || '';
  el.textContent = msg || '';
}

async function fetchJSON(url, opts){
  const res = await fetch(url, opts);
  const text = await res.text();

  if (!res.ok){
    throw new Error('HTTP ' + res.status + ' ' + url + ' - ' + text.slice(0,200));
  }

  try {
    return JSON.parse(text);
  } catch (e) {
    return text;
  }
}

function normalizeAccounts(data){
  const out = [];

  if (Array.isArray(data)){
    for (const a of data) out.push(a);
  } else if (data && typeof data === 'object'){
    for (const key of Object.keys(data)){
      const v = data[key];

      if (Array.isArray(v)){
        for (const a of v) out.push(Object.assign({platform:key}, a));
      } else if (v && typeof v === 'object'){
        out.push(v);
      }
    }
  }

  return out;
}

function normalizeJobs(data){
  if (Array.isArray(data)) return data.slice();
  if (data && typeof data === 'object') return Object.values(data);
  return [];
}

function updateStats(){
  const accounts = latestAccounts || [];
  const jobs = latestJobs || [];

  const ready = accounts.filter(function(a){
    return String(a.status || '').toLowerCase() === 'ready';
  }).length;

  const running = jobs.filter(function(j){
    const s = String(j.status || '').toLowerCase();
    return s === 'running' || s === 'queued';
  }).length;

  document.getElementById('statAccounts').textContent = accounts.length;
  document.getElementById('statReady').textContent = ready;
  document.getElementById('statJobs').textContent = jobs.length;
  document.getElementById('statRunning').textContent = running;
}

async function checkApi(){
  const b = document.getElementById('apiBadge');

  try {
    await fetchJSON('/');
    b.textContent = 'API: online';
    b.className = 'badge b-green';
  } catch (e) {
    b.textContent = 'API: offline';
    b.className = 'badge b-red';
  }
}

async function loadAccounts(){
  const box = document.getElementById('accounts');

  try {
    const data = await fetchJSON('/accounts');
    const accs = normalizeAccounts(data);

    latestAccounts = accs;
    updateStats();

    const groups = {tiktok: [], facebook: [], other: []};

    for (const a of accs){
      const p = String(a.platform || 'other').toLowerCase();
      if (!groups[p]) groups[p] = [];
      groups[p].push(a);
    }

    let html = '';
    const order = [['tiktok','TikTok'], ['facebook','Facebook'], ['other','Other']];

    for (const item of order){
      const key = item[0];
      const label = item[1];
      const list = groups[key];

      if (!list || !list.length) continue;

      html += '<div class="plat">' + esc(platformIcon(key) + ' ' + label) + '</div>';

      for (const a of list){
        const name = a.name != null ? a.name : (a.account != null ? a.account : '-');
        const status = String(a.status || 'unknown').toLowerCase();
        const locked = (status === 'locked' || status === 'error');
        const plat = a.platform || key;

        html += '<div class="row">'
          + '<div class="account-main">'
          + '<span class="name">' + esc(name) + '</span>'
          + '<span class="account-meta">' + esc(plat) + ' account</span>'
          + '</div>'
          + statusBadge(status)
          + '<span class="spacer"></span>'
          + '<button ' + (locked ? 'disabled' : '')
          + ' onclick="runAccount(\'' + jsArg(plat) + '\',\'' + jsArg(name) + '\')">▶ Run</button>'
          + '</div>';
      }
    }

    box.innerHTML = html || '<div class="empty">沒有任何帳號。</div>';

  } catch (e) {
    box.innerHTML = '<div class="empty">載入 accounts 失敗。</div>';
    notice('載入 /accounts 時發生錯誤：' + e.message, 'err');
  }
}

async function loadJobs(){
  const box = document.getElementById('jobs');

  try {
    const data = await fetchJSON('/jobs');
    const jobs = normalizeJobs(data);

    jobs.sort(function(a,b){
      return (b.created_at || 0) - (a.created_at || 0);
    });

    latestJobs = jobs;
    updateStats();

    if (!jobs.length){
      box.innerHTML = '<div class="empty">尚無任何 job。</div>';
      return;
    }

    let html = '';

    for (const j of jobs){
      const id = j.id || j.job_id || '';
      const rc = (j.return_code != null) ? ('rc=' + esc(j.return_code)) : '';
      const sel = (id === selectedJobId) ? ' selected' : '';
      const account = j.account || 'all';
      const platform = j.platform || '-';

      html += '<div class="row job-row' + sel + '" onclick="selectJob(\'' + jsArg(id) + '\')">'
        + '<div class="job-main">'
        + '<span class="name mono">' + esc(shortId(id)) + '</span>'
        + '<span class="job-meta">' + esc(platformIcon(platform)) + ' ' + esc(platform) + ' / ' + esc(account) + '</span>'
        + (j.failed_step ? '<span class="job-meta">✖ ' + esc(j.failed_step) + (j.error ? ': ' + esc(j.error) : '') + '</span>' : '')
        + '</div>'
        + '<span class="spacer"></span>'
        + statusBadge(j.status)
        + (rc ? '<span class="small mono">' + rc + '</span>' : '')
        + '</div>';
    }

    box.innerHTML = html;

  } catch (e) {
    box.innerHTML = '<div class="empty">載入 jobs 失敗。</div>';
    notice('載入 /jobs 時發生錯誤：' + e.message, 'err');
  }
}

function selectJob(id){
  if (!id) return;

  selectedJobId = id;
  document.getElementById('selLabel').textContent = 'Job: ' + id;

  loadJobs();
  loadLogs();
}

async function copyJobId(){
  if (!selectedJobId){
    notice('尚未選擇任何 job。', 'err');
    return;
  }

  try {
    await navigator.clipboard.writeText(selectedJobId);
    notice('已複製 Job ID。', 'ok');
  } catch (e) {
    notice('無法複製，Job ID：' + selectedJobId, 'err');
  }
}

async function loadLogs(){
  if (!selectedJobId) return;

  const pre = document.getElementById('logs');

  try {
    const res = await fetch('/jobs/' + encodeURIComponent(selectedJobId) + '/logs');
    const raw = await res.text();

    if (!res.ok){
      throw new Error('HTTP ' + res.status + ' - ' + raw.slice(0,200));
    }

    let out = raw;

    try {
      const parsed = JSON.parse(raw);

      if (Array.isArray(parsed)){
        out = parsed.join('\n');
      } else if (parsed && typeof parsed === 'object' && 'logs' in parsed){
        out = Array.isArray(parsed.logs) ? parsed.logs.join('\n') : String(parsed.logs);
      } else {
        out = String(parsed);
      }
    } catch (e) {
      // 若不是 JSON 則保留原始文字。
    }

    pre.textContent = out || '（log 為空）';
    pre.scrollTop = pre.scrollHeight;

  } catch (e) {
    notice('載入 logs 時發生錯誤：' + e.message, 'err');
  }
}

async function runAccount(platform, account){
  notice('正在執行 ' + platform + '/' + account + ' ...', '');

  try {
    const data = await fetchJSON(
      '/run/' + encodeURIComponent(platform) + '/' + encodeURIComponent(account),
      {method:'POST'}
    );

    if (data && data.ok === false && !(data.job_id || data.id)){
      notice('未執行 ' + platform + '/' + account + ': ' + (data.message || data.status), 'err');
      await loadJobs();
      return;
    }

    const id = (data && (data.job_id || data.id)) || null;

    if (id){
      selectedJobId = id;
      document.getElementById('selLabel').textContent = 'Job: ' + id;
    }

    notice(
      '已啟動 ' + platform + '/' + account + (id ? (' - job ' + shortId(id)) : ''),
      'ok'
    );

    await loadJobs();
    await loadLogs();

  } catch (e) {
    notice('執行 job 時發生錯誤：' + e.message, 'err');
  }
}

async function refreshAll(){
  notice('');
  await checkApi();
  await loadAccounts();
  await loadJobs();
}

// 初始化 + polling
refreshAll();

setInterval(loadJobs, 5000);

setInterval(function(){
  if (selectedJobId) loadLogs();
}, 3000);

setInterval(checkApi, 15000);
