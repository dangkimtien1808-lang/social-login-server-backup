// Targets UI：只讀寫 /api/targets，不執行任何 automation。
(function(){
  'use strict';

  const PLATFORMS = ['facebook', 'tiktok'];
  const ACTIONS = ['like', 'follow', 'add_friend'];
  const NUMBERS = ['limit_per_run', 'delay_min_seconds', 'delay_max_seconds'];
  const $ = function(id){ return document.getElementById(id); };

  function field(platform, name){
    return $(platform + '_' + name);
  }

  function setApi(ok){
    $('apiBadge').textContent = ok ? 'API: online' : 'API: error';
    $('apiBadge').className = 'badge ' + (ok ? 'b-green' : 'b-red');
  }

  function notice(node, msg, kind){
    node.className = kind || '';
    node.textContent = msg || '';
  }

  function setCount(platform){
    const n = readHashtags(platform).length;
    field(platform, 'count').textContent = n + ' hashtags / keywords';
  }

  function readHashtags(platform){
    return field(platform, 'hashtags').value.split('\n').map(function(line){
      return line.trim().replace(/^#+/, '').trim();
    }).filter(Boolean);
  }

  function fill(platform, cfg){
    field(platform, 'enabled').checked = cfg.enabled;
    field(platform, 'hashtags').value = cfg.hashtags.join('\n');
    for (const a of ACTIONS) field(platform, a).checked = cfg.actions[a];
    for (const n of NUMBERS) field(platform, n).value = cfg[n];
    setCount(platform);
  }

  function read(platform){
    const cfg = {
      enabled: field(platform, 'enabled').checked,
      hashtags: readHashtags(platform),
      actions: {}
    };
    for (const a of ACTIONS) cfg.actions[a] = field(platform, a).checked;
    for (const n of NUMBERS){
      const raw = field(platform, n).value;
      cfg[n] = raw === '' ? null : Number(raw); // 空白交由後端驗證拒絕
    }
    return cfg;
  }

  async function request(options){
    const res = await fetch('/api/targets', Object.assign({cache:'no-store'}, options));
    const data = await res.json().catch(function(){ return {}; });
    if (!res.ok) throw new Error(data.message || ('HTTP ' + res.status));
    return data;
  }

  async function load(){
    $('refreshBtn').disabled = true;
    try {
      const cfg = await request();
      for (const p of PLATFORMS){
        fill(p, cfg[p]);
        field(p, 'save').disabled = false;
        notice(field(p, 'message'));
      }
      setApi(true);
      notice($('notice'));
    } catch (e) {
      setApi(false);
      notice($('notice'), 'Không tải được cấu hình: ' + e.message, 'err');
    } finally {
      $('refreshBtn').disabled = false;
    }
  }

  // 只送出該平台，另一張卡片尚未儲存的內容不受影響
  async function save(platform){
    const message = field(platform, 'message');
    field(platform, 'save').disabled = true;
    notice(message, 'Đang lưu...');
    try {
      const data = await request({
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({[platform]: read(platform)})
      });
      fill(platform, data.config[platform]);
      notice(message, 'Đã lưu cấu hình.', 'ok');
    } catch (e) {
      notice(message, 'Lưu thất bại: ' + e.message, 'err');
    } finally {
      field(platform, 'save').disabled = false;
    }
  }

  for (const p of PLATFORMS){
    field(p, 'form').addEventListener('submit', function(event){
      event.preventDefault();
      save(p);
    });
    field(p, 'hashtags').addEventListener('input', function(){ setCount(p); });
  }
  $('refreshBtn').addEventListener('click', load);

  load();
})();
