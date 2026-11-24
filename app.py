import os
import sys
import json
import uuid
import time
import signal
import threading
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from accounts import load_accounts

# 此區塊初始化 API 應用程式與 job 管理用的記憶體。
#
# 注意：請勿以 --workers > 1 執行 uvicorn。每個 worker 都會建立自己的 scheduler，
# 這樣 9 點的排程會執行多次，同一個帳號會被重複開啟（profile 被鎖定）。
#
# 在伺服器上正式執行時請勿使用 --reload：
#   uvicorn app:app --host 0.0.0.0 --port 8000
# 使用 --reload 時，每次存檔伺服器都會重新啟動 -> jobs 清單會遺失，且
# 執行中的 job 會被 kill（見 kill_all_running_jobs）。只有在修改程式碼時才使用 --reload。

BASE_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable

LOGS_DIR = BASE_DIR / "logs"
RESULTS_DIR = BASE_DIR / "job_results"

# 視為「帳號忙碌中」的狀態，用來防止重複執行。
ACTIVE_STATUSES = {"queued", "pending", "running"}

# 單一 job 的最長執行時間（分鐘）。逾時 -> kill 整個行程樹，failed_step="timeout"。
# 可用環境變數修改，例如：JOB_TIMEOUT_MINUTES=45 uvicorn app:app ...
JOB_TIMEOUT_S = int(float(os.environ.get("JOB_TIMEOUT_MINUTES", "30")) * 60)
KILL_GRACE_S = 10  # SIGTERM 後等待一段時間再 SIGKILL

jobs = {}
jobs_lock = threading.Lock()

# job_id -> 執行中 job 的 Popen，用於逾時或伺服器關閉/reload 時 kill
running_procs = {}

# 排程使用台灣時間 (UTC+8)。伺服器設定為 UTC，所以必須在此明確指定時區。
scheduler = BackgroundScheduler(timezone="Asia/Taipei")


@asynccontextmanager
async def lifespan(_app):
    # Startup：啟動 scheduler。Shutdown：關閉 scheduler + kill 所有執行中的 job，
    # 以免 uvicorn 關閉/reload 時留下「孤兒」瀏覽器佔用 profile。
    if not scheduler.running:
        scheduler.start()
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        kill_all_running_jobs()


app = FastAPI(title="Social Login Test Runner", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# 建立 job 執行指令的區塊
# 此區塊建立指令，讓後端在背景呼叫自動化程式。
# 保留 xvfb-run，因為伺服器沒有螢幕，headless=False 的瀏覽器需要虛擬螢幕。

def build_command(platform: str, account: str | None = None):
    if platform not in {"tiktok", "facebook"}:
        raise ValueError("platform must be 'tiktok' or 'facebook'")

    cmd = [
        "xvfb-run",
        "-a",
        "-s",
        "-screen 0 1920x1080x24",
        PYTHON,
        "run_all.py",
        platform,
    ]

    if account:
        cmd.append(account)

    return cmd


# 更新 job 狀態的區塊
# 此區塊用於安全地更新 job 狀態。

def set_job(job_id, **kwargs):
    with jobs_lock:
        jobs[job_id].update(kwargs)


def _is_account_running_unlocked(platform, account):
    for job in jobs.values():
        if job.get("platform") != platform:
            continue
        if str(job.get("status", "")).lower() not in ACTIVE_STATUSES:
            continue
        # account=None 是舊版執行整個平台的 job -> 視為所有帳號都忙碌中
        if job.get("account") in (None, account):
            return True
    return False


def is_account_running(platform, account):
    # 若相同 platform/account 已有 queued/pending/running 的 job 則為 True。
    with jobs_lock:
        return _is_account_running_unlocked(platform, account)


# kill 行程樹的區塊
# 行程鏈：xvfb-run -> run_all.py -> test_*.py -> playwright driver -> 瀏覽器。
# Playwright 在獨立的 process group 開啟瀏覽器，所以無法依 group kill；
# 這裡透過 /proc (Linux) 取得所有子孫行程後全部 kill。

def _descendant_pids(root_pid):
    children = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as f:
                    stat = f.read()
                # 行程名稱位於 (...) 內，可能含有空白 -> 從最後一個 ')' 之後切割
                ppid = int(stat.rsplit(")", 1)[1].split()[1])
                children.setdefault(ppid, []).append(int(entry))
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return []

    result, stack = [], [root_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            result.append(child)
            stack.append(child)
    return result


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_process_tree(root_pid, grace_s=KILL_GRACE_S):
    pids = [root_pid] + _descendant_pids(root_pid)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.time() + grace_s
    while time.time() < deadline and any(_pid_alive(p) for p in pids):
        time.sleep(0.5)

    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def kill_all_running_jobs():
    with jobs_lock:
        items = list(running_procs.items())
    for job_id, process in items:
        if process.poll() is None:
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["killed_reason"] = "server_shutdown"
                    jobs[job_id]["logs"].append("[backend] 伺服器關閉/reload -> kill job。")
            kill_process_tree(process.pid, grace_s=3)


def _on_job_timeout(job_id, process):
    if process.poll() is not None:
        return
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        job["killed_reason"] = "timeout"
        job["logs"].append(f"[backend] Job 超過 {JOB_TIMEOUT_S / 60:g} 分鐘 -> kill 整個行程樹。")
    kill_process_tree(process.pid)


# 檢查 profile 鎖定的區塊
# 偵測 profile 是否被其他瀏覽器佔用（包含 jobs 不知道的「孤兒」工作階段）。
#   Firefox: <profile>/lock    -> "IP:+PID"
#   Chrome : <profile>/SingletonLock -> "hostname-PID"

def profile_locked_by(profile_dir):
    path = Path(profile_dir)
    if not path.is_absolute():
        path = BASE_DIR / path

    for name, sep in (("lock", "+"), ("SingletonLock", "-")):
        link = path / name
        if not link.is_symlink():
            continue
        try:
            pid = int(os.readlink(link).rsplit(sep, 1)[1])
        except (OSError, ValueError, IndexError):
            continue
        if _pid_alive(pid):
            return pid
    return None


# 監控 job 的區塊
# 即時讀取子行程的 log，等待結束後決定 success/failed，
# 依據 returncode 以及自動化程式寫出的結果檔 (JOB_RESULT_PATH)。

def monitor_job(job_id, process, log_fp):
    with jobs_lock:
        log_lines = jobs[job_id]["logs"]
        result_file = jobs[job_id].get("result_file")

    timer = threading.Timer(JOB_TIMEOUT_S, _on_job_timeout, args=(job_id, process))
    timer.daemon = True
    timer.start()

    try:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                log_lines.append(line)
                try:
                    log_fp.write(line + "\n")
                    log_fp.flush()
                except Exception:
                    pass

            returncode = process.wait()
        finally:
            timer.cancel()
            with jobs_lock:
                running_procs.pop(job_id, None)
            try:
                log_fp.close()
            except Exception:
                pass

        update = {
            "returncode": returncode,
            "return_code": returncode,  # dashboard 會讀取此欄位
            "finished_at": time.time(),
        }

        with jobs_lock:
            killed_reason = jobs[job_id].get("killed_reason")

        result = None
        if result_file and os.path.exists(result_file):
            with open(result_file, encoding="utf-8") as f:
                result = json.load(f)

        if killed_reason:
            update["status"] = "failed"
            update["failed_step"] = killed_reason
            update["completed_steps"] = (result or {}).get("completed_steps") or []
            update["error"] = (
                f"Job 執行超過 {JOB_TIMEOUT_S / 60:g} 分鐘，已被 kill。"
                if killed_reason == "timeout"
                else "Job 執行期間伺服器關閉/reload，已 kill job。"
            )
            if result and result.get("failed_step"):
                update["error"] += f" 先前出錯的步驟：{result['failed_step']}: {result.get('error')}"
        elif result is not None:
            update["failed_step"] = result.get("failed_step")
            update["error"] = result.get("error")
            update["completed_steps"] = result.get("completed_steps") or []

            if result.get("ok") is True and returncode == 0:
                update["status"] = "success"
            else:
                update["status"] = "failed"
                if result.get("ok") is True and returncode != 0:
                    update["failed_step"] = update["failed_step"] or "process_exit"
                    update["error"] = update["error"] or f"Process exited with code {returncode}."
        else:
            update["status"] = "failed"
            update["failed_step"] = "result_missing"
            update["error"] = "Automation process ended but result file was not created."

        set_job(job_id, **update)

    except Exception as e:
        log_lines.append(f"[backend] ERROR monitor_job: {e}")
        set_job(
            job_id,
            status="failed",
            failed_step="monitor_job",
            error=f"{type(e).__name__}: {e}",
            finished_at=time.time(),
        )


# 為單一帳號啟動 job 的區塊（Run 按鈕、/run/{platform} 與 scheduler 共用）

def find_account(platform, account):
    try:
        for a in load_accounts(platform):
            if a["name"] == account:
                return a
    except Exception:
        pass
    return None


def account_exists(platform, account):
    return find_account(platform, account) is not None


def start_account_job(platform: str, account: str, source: str = "manual"):
    cmd = build_command(platform, account)  # platform 錯誤時拋出 ValueError

    acc = find_account(platform, account)
    if acc is None:
        return {
            "ok": False,
            "status": "skipped",
            "message": f"帳號 {platform}/{account} 不在 accounts.json 中。",
        }

    lock_pid = profile_locked_by(acc["profile_dir"])
    if lock_pid:
        return {
            "ok": False,
            "status": "skipped",
            "message": (
                f"Profile {acc['profile_dir']} 正被 PID {lock_pid} 的行程佔用"
                f"（可能是卡住的舊工作階段）。請 kill 該 PID 後再重新執行。"
            ),
        }

    job_id = str(uuid.uuid4())

    LOGS_DIR.mkdir(exist_ok=True)
    RESULTS_DIR.mkdir(exist_ok=True)
    log_file = str(LOGS_DIR / f"{job_id}.log")
    result_file = str(RESULTS_DIR / f"{job_id}.json")

    # 在同一次鎖定中檢查重複並登記 job，讓 2 個同時發出的 request
    # （例如 scheduler 與 Run 按鈕）不會同時通過。
    with jobs_lock:
        if _is_account_running_unlocked(platform, account):
            return {
                "ok": False,
                "status": "skipped",
                "message": f"帳號 {platform}/{account} 正在執行中，略過以避免 profile 被鎖定。",
            }

        jobs[job_id] = {
            "id": job_id,
            "platform": platform,
            "account": account,
            "source": source,
            "status": "pending",
            "created_at": time.time(),
            "command": " ".join(cmd),
            "log_file": log_file,
            "result_file": result_file,
            "failed_step": None,
            "error": None,
            "completed_steps": [],
            "returncode": None,
            "finished_at": None,
            "logs": [],
        }

    env = os.environ.copy()
    env.update(
        PYTHONUNBUFFERED="1",
        PYTHONIOENCODING="utf-8",
        JOB_ID=job_id,
        JOB_RESULT_PATH=result_file,
    )

    log_fp = None
    try:
        log_fp = open(log_file, "w", encoding="utf-8")
        process = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except Exception as e:
        if log_fp:
            log_fp.close()
        set_job(
            job_id,
            status="failed",
            failed_step="start_process",
            error=f"{type(e).__name__}: {e}",
            finished_at=time.time(),
        )
        with jobs_lock:
            return {"ok": False, "job_id": job_id, **jobs[job_id]}

    with jobs_lock:
        running_procs[job_id] = process
    set_job(job_id, status="running", started_at=time.time(), pid=process.pid)

    threading.Thread(
        target=monitor_job, args=(job_id, process, log_fp), daemon=True
    ).start()

    with jobs_lock:
        return {"ok": True, "job_id": job_id, **jobs[job_id]}


# 自動排程的區塊
# 若要變更 9 點執行的帳號群組，請修改下方的 GROUP_9AM 清單。

GROUP_9AM = [
    ("tiktok", "acc3"),
    #("facebook", "acc2"),
    #("facebook", "acc3"),
]

GROUP_13PM = [
    ("tiktok", "acc4"),
]

GROUP_GAP_SECONDS = 120  # 兩個帳號之間的間隔，避免同時大量開啟瀏覽器


def run_group(group, source):
    for i, (platform, account) in enumerate(group):
        if i > 0:
            time.sleep(GROUP_GAP_SECONDS)
        try:
            res = start_account_job(platform, account, source=source)
            print(f"[scheduler] {platform}/{account}: {res.get('status')} {res.get('message', '')}", flush=True)
        except Exception as e:
            print(f"[scheduler] {platform}/{account}: ERROR {e}", flush=True)


def run_group_9am():
    run_group(GROUP_9AM, source="schedule_9am")


def run_group_13pm():
    run_group(GROUP_13PM, source="schedule_13pm")


scheduler.add_job(
    run_group_9am,
    # 明確傳入時區：CronTrigger 預設使用主機時區（伺服器目前為 UTC）
    CronTrigger(hour=9, minute=0, timezone=scheduler.timezone),
    id="group_9am",
    name="run_group_9am",
    replace_existing=True,
)

scheduler.add_job(
    run_group_13pm,
    CronTrigger(hour=13, minute=0, timezone=scheduler.timezone),
    id="group_13pm",
    name="run_group_13pm",
    replace_existing=True,
)


# 基本 API 區塊
# 這是檢查伺服器是否仍在運作的 endpoint。

@app.get("/")
def root():
    return {
        "name": "Social Login Test Runner",
        "status": "ok",
    }


# 帳號管理 API 區塊
# 此區塊回傳帳號清單與目前狀態，供 dashboard 顯示。

@app.get("/accounts")
def accounts():
    result = []

    for platform in ["tiktok", "facebook"]:
        try:
            for acc in load_accounts(platform):
                status = "ready"

                # 依照專案目前的實際狀態
                if platform == "facebook" and acc["name"] == "acc1":
                    status = "need_verification"
                if platform == "facebook" and acc["name"] == "acc4":
                    status = "locked"

                result.append({
                    "platform": platform,
                    "name": acc["name"],
                    "status": status,
                })

        except Exception as e:
            result.append({
                "platform": platform,
                "name": "-",
                "status": "error",
                "error": str(e),
            })

    return result


# 執行與查看 job 的 API 區塊
# 此區塊為控制 job 的 API：建立 job、執行 job、查看 job 清單、查看詳情與 log。

PLATFORM_LAUNCH_DELAY_S = 5  # 與 run_all.py 相同：每次開啟瀏覽器之間的間隔


@app.post("/run/{platform}")
def run_platform(platform: str):
    # 執行該平台所有帳號：每個帳號一個獨立 job（有各自的結果檔，
    # 並防止重複執行），在背景依序啟動。
    if platform not in {"tiktok", "facebook"}:
        raise HTTPException(status_code=400, detail="platform must be 'tiktok' or 'facebook'")

    try:
        names = [a["name"] for a in load_accounts(platform)]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"無法讀取 accounts：{e}")

    def _launch_all():
        for i, name in enumerate(names):
            if i > 0:
                time.sleep(PLATFORM_LAUNCH_DELAY_S)
            start_account_job(platform, name, source="manual_platform")

    threading.Thread(target=_launch_all, daemon=True).start()

    return {
        "ok": True,
        "status": "running",
        "platform": platform,
        "accounts": names,
        "message": f"正在啟動 {len(names)} 個 {platform} 帳號，每個帳號一個 job。",
    }


@app.api_route("/run/{platform}/{account}", methods=["GET", "POST"])
def run_account(platform: str, account: str):
    try:
        return start_account_job(platform, account, source="manual")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/jobs")
def list_jobs():
    with jobs_lock:
        return list(jobs.values())


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return job


@app.get("/jobs/{job_id}/logs", response_class=PlainTextResponse)
def get_job_logs(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return "\n".join(job.get("logs", []))


# 排程 API 區塊

@app.get("/schedules")
def list_schedules():
    return [
        {
            "id": j.id,
            "name": j.name,
            "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None,
        }
        for j in scheduler.get_jobs()
    ]


@app.get("/schedule/test/9am")
def test_schedule_9am():
    threading.Thread(target=run_group_9am, daemon=True).start()
    return {
        "ok": True,
        "message": (
            f"已立即觸發 9 點群組：{GROUP_9AM}，"
            f"每個帳號間隔 {GROUP_GAP_SECONDS} 秒。請至 /jobs 查看進度。"
        ),
    }


# Dashboard HTML 區塊
# 這是 dashboard 介面，讓使用者點擊即可執行 job 並查看 log，不必手動呼叫 API。

DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Social Login Test Runner</title>

<link rel="stylesheet" href="/static/dashboard.css?v=1">
</head>

<body>
<div class="app">

  <aside class="sidebar">
    <div class="brand">
      <div class="brand-logo">SL</div>
      <div class="brand-title">
        <strong>Social Runner</strong>
        <span>Automation Dashboard</span>
      </div>
    </div>

    <div class="nav-block">
      <div class="nav-label">Menu</div>

      <div class="nav-item active">
        <span class="nav-icon">📊</span>
        <span>Dashboard</span>
      </div>

      <div class="nav-item">
        <span class="nav-icon">👤</span>
        <span>Accounts</span>
      </div>

      <div class="nav-item">
        <span class="nav-icon">⚙️</span>
        <span>Jobs</span>
      </div>

      <div class="nav-item">
        <span class="nav-icon">🧾</span>
        <span>Logs</span>
      </div>
    </div>

    <div class="side-footer">
      <span id="apiBadge" class="badge b-gray">API: checking...</span>
      <span class="small">FastAPI backend monitor</span>
    </div>
  </aside>

  <main class="main">

    <div class="mobile-brand">
      <div class="brand-logo">SL</div>
      <div class="brand-title">
        <strong>Social Runner</strong>
        <span>Automation Dashboard</span>
      </div>
    </div>

    <section class="topbar">
      <div class="page-title">
        <h1>Social Login Test Runner</h1>
        <p>在同一個介面上管理帳號、執行自動化 job 並即時查看 log。</p>
      </div>

      <div class="top-actions">
        <button class="ghost" onclick="refreshAll()">↻ Refresh All</button>
      </div>
    </section>

    <section class="hero">
      <div class="hero-content">
        <div>
          <h2>Automation Control Center</h2>
          <p>
            Dashboard 用於查看帳號清單、針對各帳號執行 job、
            檢查 job 狀態並讀取自動化行程的 log。
          </p>
        </div>

        <div class="hero-meta">
          <span class="badge b-green">Live UI</span>
          <span class="badge b-amber">Yellow White</span>
        </div>
      </div>
    </section>

    <div id="notice"></div>

    <section class="stats">
      <div class="stat-card">
        <div class="stat-label">Accounts</div>
        <div id="statAccounts" class="stat-value">--</div>
        <div class="stat-hint">已讀取的帳號總數</div>
      </div>

      <div class="stat-card">
        <div class="stat-label">Ready</div>
        <div id="statReady" class="stat-value">--</div>
        <div class="stat-hint">可執行的帳號</div>
      </div>

      <div class="stat-card">
        <div class="stat-label">Jobs</div>
        <div id="statJobs" class="stat-value">--</div>
        <div class="stat-hint">已建立的 job 總數</div>
      </div>

      <div class="stat-card">
        <div class="stat-label">Running</div>
        <div id="statRunning" class="stat-value">--</div>
        <div class="stat-hint">執行中 / 等待中的 job</div>
      </div>
    </section>

    <section class="grid">
      <div class="card">
        <div class="card-head">
          <div class="card-title">
            <div class="card-title-icon">👤</div>
            <div>
              <h2>Accounts</h2>
              <div class="card-subtitle">依平台分類的帳號清單</div>
            </div>
          </div>

          <button class="ghost" onclick="loadAccounts()">↻ Refresh</button>
        </div>

        <div class="card-body">
          <div id="accounts">
            <div class="empty">正在載入帳號清單...</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div class="card-title">
            <div class="card-title-icon">⚙️</div>
            <div>
              <h2>Jobs</h2>
              <div class="card-subtitle">最新的 job 與執行狀態</div>
            </div>
          </div>

          <button class="ghost" onclick="loadJobs()">↻ Refresh</button>
        </div>

        <div class="card-body">
          <div id="jobs">
            <div class="empty">正在載入 job 清單...</div>
          </div>
        </div>
      </div>
    </section>

    <section class="card logs-card">
      <div class="card-head">
        <div class="card-title">
          <div class="card-title-icon">🧾</div>
          <div>
            <h2>Logs</h2>
            <div id="selLabel" class="card-subtitle mono">No job selected</div>
          </div>
        </div>

        <div class="toolbar">
          <button class="ghost" onclick="loadLogs()">↻ Refresh Logs</button>
          <button class="ghost" onclick="copyJobId()">Copy Job ID</button>
        </div>
      </div>

      <div class="card-body">
        <div class="log-shell">
          <pre id="logs">選擇一個 job 以查看 log...</pre>
        </div>
      </div>
    </section>

  </main>
</div>

<script src="/static/dashboard.js?v=1"></script>
</body>
</html>"""


# 回傳 dashboard 的 route 區塊
# 此區塊提供系統的網頁介面。

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML
