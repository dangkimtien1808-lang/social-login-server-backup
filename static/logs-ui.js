// Logs UI：唯讀，只呼叫 GET /jobs 與 GET /jobs/{job_id}/logs。
(function(){
  'use strict';

  const $ = function(id){ return document.getElementById(id); };
  let jobs = [];

  // 一律使用 textContent，避免資料被當成 HTML 執行
  function el(tag, cls, text){
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function str(v){
    return (typeof v === 'string' && v) || typeof v === 'number' ? String(v) : '';
  }

  function badge(status){
    const s = status.toLowerCase();
    let cls = 'b-gray';
    if (s === 'success') cls = 'b-green';
    else if (s === 'running' || s === 'queued' || s === 'pending') cls = 'b-amber';
    else if (s === 'failed' || s === 'error') cls = 'b-red';
    return el('span', 'badge ' + cls, s || '-');
  }

  // 回傳毫秒；支援 epoch 秒、epoch 毫秒與日期字串
  function toMs(v){
    if (v == null || v === '') return null;
    const n = Number(v);
    const ms = Number.isFinite(n) ? (n < 1e12 ? n * 1000 : n) : Date.parse(v);
    return ms > 0 && !Number.isNaN(new Date(ms).getTime()) ? ms : null;
  }

  function formatTime(ms){
    return ms == null ? '-' : new Date(ms).toLocaleString('zh-TW', {
      timeZone:'Asia/Taipei', hour12:false, month:'2-digit', day:'2-digit',
      hour:'2-digit', minute:'2-digit', second:'2-digit'
    });
  }

  function toList(data){
    if (Array.isArray(data)) return data;
    if (!data || typeof data !== 'object') return [];
    if (Array.isArray(data.jobs)) return data.jobs;
    const map = data.jobs && typeof data.jobs === 'object' ? data.jobs : data;
    return Object.entries(map).filter(function(e){
      return e[1] && typeof e[1] === 'object' && !Array.isArray(e[1]);
    }).map(function(e){ return Object.assign({id:e[0]}, e[1]); });
  }

  // 只保留要顯示的欄位，其餘（command、credential 等）一律捨棄
  function normalize(job){
    return {
      id: str(job.id) || str(job.job_id),
      platform: str(job.platform),
      account: str(job.account),
      status: str(job.status),
      started: toMs(job.started_at || job.start_time || job.created_at),
      begin: toMs(job.started_at || job.start_time),
      end: toMs(job.finished_at || job.ended_at || job.end_time),
      duration: job.duration
    };
  }

  function parseJobs(data){
    return toList(data).filter(function(j){
      return j && typeof j === 'object';
    }).map(normalize).sort(function(a, b){
      return (b.started || 0) - (a.started || 0);
    });
  }

  function notice(msg){
    $('notice').className = msg ? 'err' : '';
    $('notice').textContent = msg || '';
  }

  function setApi(ok){
    $('apiBadge').textContent = ok ? 'API: online' : 'API: error';
    $('apiBadge').className = 'badge ' + (ok ? 'b-green' : 'b-red');
  }

  async function fetchJobs(){
    try {
      const res = await fetch('/jobs', {cache:'no-store'});
      if (!res.ok) throw new Error('HTTP ' + res.status);
      jobs = parseJobs(await res.json().catch(function(){ return []; }));
      setApi(true);
      notice('');
    } catch (e) {
      setApi(false);
      notice('Không tải được danh sách job: ' + e.message);
    }
  }

  const EMPTY = 'Chọn một job để xem log.';
  let selectedId = new URLSearchParams(location.search).get('job_id') || '';
  let logText = '';      // 已遮蔽的完整 log
  let shownText = '';    // 目前畫面顯示的 log（Copy / Download 使用）
  let message = EMPTY;   // 沒有 log 可顯示時的訊息
  let requestNo = 0;

  // 盡力遮蔽 log 內常見的 credential 格式
  function mask(text){
    return text
      .replace(/(\b(?:set-)?cookies?["']?\s*[:=]\s*)(?=[^\n]*=)[^\n]+/gi, '$1***')
      .replace(/(\b(?:authorization)["']?\s*[:=]\s*)[^\n]+/gi, '$1***')
      .replace(/(\bbearer\s+)[\w.~+\/=-]+/gi, '$1***')
      .replace(/([\w-]*(?:passw(?:or)?d|passwd|token|secret|credential|_cred|api_?key|sessionid|c_user|datr)\w*["']?\s*[:=]\s*)("[^"\n]*"|'[^'\n]*'|[^\s,;&"'}]+)/gi, '$1***')
      .replace(/(\bxs=)[^\s;,&]+/g, '$1***')
      .replace(/(["']value["']\s*:\s*)("[^"\n]*"|'[^'\n]*')/gi, '$1***');
  }

  function lineText(v){
    if (v == null) return '';
    if (typeof v !== 'object') return String(v);
    const msg = v.message != null ? v.message : (v.text != null ? v.text : v.line);
    return msg != null && typeof msg !== 'object' ? String(msg) : JSON.stringify(v);
  }

  function parseLogs(raw){
    let data;
    try { data = JSON.parse(raw); } catch (e) { return raw; }
    if (data && typeof data === 'object' && !Array.isArray(data)){
      if (data.logs != null) data = data.logs;
      else if (data.lines != null) data = data.lines;
      else return JSON.stringify(data, null, 2);
    }
    if (Array.isArray(data)) return data.map(lineText).join('\n');
    return typeof data === 'string' ? data : raw;
  }

  function renderLog(){
    const q = $('searchInput').value.trim().toLowerCase();
    shownText = !q ? logText : logText.split('\n').filter(function(line){
      return line.toLowerCase().includes(q);
    }).join('\n');

    $('selLabel').textContent = selectedId || 'No job selected';
    $('logEmpty').hidden = !!shownText;
    $('logText').hidden = !shownText;
    $('logEmpty').textContent = logText && !shownText
      ? 'Không có dòng log nào khớp.' : message;
    $('logText').textContent = shownText;
    $('copyBtn').disabled = $('downloadBtn').disabled = !shownText;
  }

  function renderSessions(){
    const platform = $('platformFilter').value;
    const list = jobs.filter(function(j){
      return j.id && (!platform || j.platform.toLowerCase() === platform);
    });

    $('sessionCount').textContent = list.length + ' / ' + jobs.length + ' jobs';
    $('sessions').replaceChildren.apply($('sessions'), list.length ? list.map(function(job){
      const row = el('button', 'row' + (job.id === selectedId ? ' selected' : ''));
      row.type = 'button';
      row.setAttribute('aria-pressed', String(job.id === selectedId));
      const main = el('span', 'job-main');
      main.append(
        el('span', 'name', (job.platform || '-') + ' / ' + (job.account || '-')),
        el('span', 'job-meta', formatTime(job.started))
      );
      row.append(main, badge(job.status));
      row.addEventListener('click', function(){ select(job.id); });
      return row;
    }) : [el('div', 'empty', 'Không có job nào.')]);
  }

  async function loadLogs(){
    if (!selectedId) return;
    const no = ++requestNo;
    let text = '';
    let msg = 'Job này chưa có log.';

    try {
      const res = await fetch('/jobs/' + encodeURIComponent(selectedId) + '/logs', {cache:'no-store'});
      if (res.status === 404) msg = 'Không tìm thấy job (có thể server đã khởi động lại).';
      else if (!res.ok) throw new Error('HTTP ' + res.status);
      else text = mask(parseLogs(await res.text()));
    } catch (e) {
      msg = 'Không tải được log: ' + e.message;
    }

    if (no !== requestNo) return; // 已選擇其他 job
    logText = text;
    message = msg;
    renderLog();
  }

  function select(id){
    selectedId = id;
    logText = '';
    message = 'Đang tải log...';
    history.replaceState(null, '', '/logs-ui?job_id=' + encodeURIComponent(id));
    renderSessions();
    renderLog();
    return loadLogs();
  }

  async function load(){
    $('refreshBtn').disabled = true;
    await Promise.all([fetchJobs().then(renderSessions), loadLogs()]);
    $('refreshBtn').disabled = false;
  }

  async function copyLog(){
    try {
      await navigator.clipboard.writeText(shownText);
      $('notice').className = 'ok';
      $('notice').textContent = 'Đã copy log.';
    } catch (e) {
      notice('Không copy được log (trình duyệt chặn clipboard).');
    }
  }

  function downloadLog(){
    const url = URL.createObjectURL(new Blob([shownText], {type:'text/plain;charset=utf-8'}));
    const a = el('a');
    a.href = url;
    a.download = 'job-' + selectedId.replace(/[^\w.-]/g, '_') + '.txt';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
  }

  $('refreshBtn').addEventListener('click', load);
  $('platformFilter').addEventListener('change', renderSessions);
  $('searchInput').addEventListener('input', renderLog);
  $('copyBtn').addEventListener('click', copyLog);
  $('downloadBtn').addEventListener('click', downloadLog);

  if (selectedId) message = 'Đang tải log...';
  renderLog();
  load();
})();
