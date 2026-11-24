// Jobs UI：唯讀，只呼叫 GET /jobs。
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

  function formatDuration(job){
    if (typeof job.duration === 'string' && job.duration && isNaN(job.duration)) return job.duration;
    let s = job.duration != null && job.duration !== '' ? Number(job.duration) : NaN;
    if (!Number.isFinite(s)){
      const end = job.end || (job.status.toLowerCase() === 'running' ? Date.now() : null);
      if (!job.begin || !end) return '-';
      s = (end - job.begin) / 1000;
    }
    s = Math.max(0, Math.round(s));
    const m = Math.floor(s / 60);
    const h = Math.floor(m / 60);
    if (h) return h + 'h ' + (m % 60) + 'm';
    return m ? m + 'm ' + (s % 60) + 's' : s + 's';
  }

  function matchStatus(job, wanted){
    const s = job.status.toLowerCase();
    // 後端以 pending 表示排隊中
    return !wanted || s === wanted || (wanted === 'queued' && s === 'pending');
  }

  function cell(child){
    const td = el('td');
    td.appendChild(child);
    return td;
  }

  function buildRow(job){
    const tr = el('tr');
    let action = el('span', 'small', '-');
    if (job.id){
      action = el('a', 'jl-link', 'View Logs');
      action.href = '/logs-ui?job_id=' + encodeURIComponent(job.id);
    }
    tr.append(
      cell(el('span', 'mono', job.id || '-')),
      cell(el('span', null, job.platform || '-')),
      cell(el('span', 'name', job.account || '-')),
      cell(badge(job.status)),
      cell(el('span', 'small', formatTime(job.started))),
      cell(el('span', 'small', formatDuration(job))),
      cell(action)
    );
    return tr;
  }

  function render(){
    const platform = $('platformFilter').value;
    const status = $('statusFilter').value;
    const list = jobs.filter(function(j){
      return (!platform || j.platform.toLowerCase() === platform) && matchStatus(j, status);
    });

    $('jobCount').textContent = list.length + ' / ' + jobs.length + ' recent jobs';
    if (list.length){
      $('jobRows').replaceChildren.apply($('jobRows'), list.map(buildRow));
      return;
    }
    const td = el('td', 'small', 'Không có job nào.');
    td.colSpan = 7;
    const tr = el('tr');
    tr.appendChild(td);
    $('jobRows').replaceChildren(tr);
  }

  async function load(){
    $('refreshBtn').disabled = true;
    await fetchJobs();
    render();
    $('refreshBtn').disabled = false;
  }

  $('refreshBtn').addEventListener('click', load);
  $('platformFilter').addEventListener('change', render);
  $('statusFilter').addEventListener('change', render);

  load();
})();
