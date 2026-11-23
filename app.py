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

<style>
  :root{
    --bg:#fffaf0;
    --bg-2:#fff7df;
    --panel:#ffffff;
    --panel-soft:#fffbeb;
    --panel-yellow:#fff4c2;
    --border:#f1d98a;
    --border-soft:#f7e7b2;

    --text:#2f2508;
    --muted:#8a6f25;
    --muted-2:#a58b3e;

    --accent:#d99a00;
    --accent-2:#f5b800;
    --accent-dark:#a86f00;
    --accent-soft:#fff2bd;

    --green:#18864b;
    --green-bg:#e8f8ef;
    --amber:#d99000;
    --amber-bg:#fff4d6;
    --red:#d93025;
    --red-bg:#fdeceb;
    --purple:#7c3aed;
    --purple-bg:#f2e9ff;
    --gray:#6b7280;
    --gray-bg:#f3f4f6;

    --shadow:0 18px 45px rgba(134, 94, 0, .14);
    --shadow-soft:0 10px 28px rgba(134, 94, 0, .10);

    --radius:22px;
    --radius-sm:15px;
  }

  *{
    box-sizing:border-box;
  }

  html{
    min-height:100%;
    background:var(--bg);
  }

  body{
    margin:0;
    min-height:100vh;
    font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
                "Segoe UI", Roboto, Arial, sans-serif;
    color:var(--text);
    background:
      radial-gradient(circle at top left, rgba(245,184,0,.25), transparent 32%),
      radial-gradient(circle at top right, rgba(255,224,128,.36), transparent 34%),
      linear-gradient(180deg, #fffaf0 0%, #fffef8 48%, #fff8df 100%);
    line-height:1.5;
  }

  button,
  input,
  textarea{
    font:inherit;
  }

  button{
    cursor:pointer;
  }

  .app{
    display:grid;
    grid-template-columns:270px minmax(0,1fr);
    min-height:100vh;
  }

  .sidebar{
    position:sticky;
    top:0;
    height:100vh;
    padding:22px 18px;
    background:rgba(255,255,255,.72);
    border-right:1px solid var(--border-soft);
    backdrop-filter:blur(18px);
    box-shadow:8px 0 28px rgba(134,94,0,.06);
  }

  .brand{
    display:flex;
    align-items:center;
    gap:12px;
    margin-bottom:28px;
  }

  .brand-logo{
    width:46px;
    height:46px;
    display:grid;
    place-items:center;
    border-radius:16px;
    color:#3b2a00;
    font-weight:950;
    letter-spacing:-.05em;
    background:linear-gradient(135deg,#ffd84d,#f5b800);
    box-shadow:0 14px 32px rgba(217,154,0,.25);
    border:1px solid rgba(168,111,0,.14);
  }

  .brand-title{
    min-width:0;
  }

  .brand-title strong{
    display:block;
    color:#2f2508;
    font-size:15px;
    letter-spacing:.01em;
  }

  .brand-title span{
    display:block;
    margin-top:2px;
    color:var(--muted);
    font-size:12px;
  }

  .nav-block{
    margin-top:18px;
  }

  .nav-label{
    margin:0 0 8px 10px;
    color:var(--muted-2);
    font-size:11px;
    font-weight:850;
    text-transform:uppercase;
    letter-spacing:.13em;
  }

  .nav-item{
    display:flex;
    align-items:center;
    gap:10px;
    padding:12px 12px;
    margin-bottom:8px;
    border-radius:15px;
    color:#6f5818;
    background:transparent;
    border:1px solid transparent;
  }

  .nav-item.active{
    color:#3b2a00;
    background:linear-gradient(135deg,#fff2bd,#fffaf0);
    border-color:var(--border-soft);
    box-shadow:var(--shadow-soft);
  }

  .nav-icon{
    width:30px;
    height:30px;
    display:grid;
    place-items:center;
    border-radius:11px;
    background:#fff6d9;
    border:1px solid #f2df9a;
  }

  .side-footer{
    position:absolute;
    left:18px;
    right:18px;
    bottom:18px;
    padding:15px;
    border:1px solid var(--border-soft);
    border-radius:18px;
    background:linear-gradient(180deg,#ffffff,#fff9e8);
    box-shadow:var(--shadow-soft);
  }

  .side-footer .small{
    display:block;
    margin-top:8px;
  }

  .main{
    min-width:0;
    padding:28px;
  }

  .mobile-brand{
    display:none;
  }

  .topbar{
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:16px;
    margin-bottom:20px;
  }

  .page-title h1{
    margin:0;
    color:#2f2508;
    font-size:30px;
    line-height:1.15;
    letter-spacing:-.045em;
  }

  .page-title p{
    margin:7px 0 0;
    color:var(--muted);
    font-size:14px;
  }

  .top-actions{
    display:flex;
    align-items:center;
    gap:10px;
    flex-wrap:wrap;
  }

  .hero{
    position:relative;
    overflow:hidden;
    padding:24px;
    margin-bottom:18px;
    border:1px solid var(--border-soft);
    border-radius:var(--radius);
    background:
      linear-gradient(135deg, rgba(255,216,77,.66), rgba(255,255,255,.82)),
      #ffffff;
    box-shadow:var(--shadow);
  }

  .hero::after{
    content:"";
    position:absolute;
    width:290px;
    height:290px;
    right:-105px;
    top:-120px;
    border-radius:999px;
    background:radial-gradient(circle, rgba(245,184,0,.42), transparent 68%);
    pointer-events:none;
  }

  .hero::before{
    content:"";
    position:absolute;
    width:160px;
    height:160px;
    left:48%;
    bottom:-90px;
    border-radius:999px;
    background:radial-gradient(circle, rgba(255,255,255,.95), transparent 70%);
    pointer-events:none;
  }

  .hero-content{
    position:relative;
    z-index:1;
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:18px;
  }

  .hero h2{
    margin:0;
    color:#302400;
    font-size:21px;
    letter-spacing:-.025em;
  }

  .hero p{
    margin:8px 0 0;
    max-width:760px;
    color:#70570d;
    font-size:14px;
  }

  .hero-meta{
    display:flex;
    align-items:center;
    gap:10px;
    flex-wrap:wrap;
  }

  .stats{
    display:grid;
    grid-template-columns:repeat(4,minmax(0,1fr));
    gap:14px;
    margin-bottom:18px;
  }

  .stat-card{
    padding:17px;
    border-radius:var(--radius-sm);
    border:1px solid var(--border-soft);
    background:rgba(255,255,255,.86);
    box-shadow:var(--shadow-soft);
  }

  .stat-label{
    color:var(--muted);
    font-size:12px;
    font-weight:850;
    text-transform:uppercase;
    letter-spacing:.08em;
  }

  .stat-value{
    margin-top:8px;
    color:#332600;
    font-size:28px;
    font-weight:950;
    letter-spacing:-.045em;
  }

  .stat-hint{
    margin-top:3px;
    color:var(--muted-2);
    font-size:12px;
  }

  .grid{
    display:grid;
    grid-template-columns:minmax(0,1fr) minmax(0,1fr);
    gap:18px;
  }

  .card{
    overflow:hidden;
    border:1px solid var(--border-soft);
    border-radius:var(--radius);
    background:rgba(255,255,255,.88);
    box-shadow:var(--shadow);
  }

  .card + .card{
    margin-top:18px;
  }

  .card-head{
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:12px;
    padding:17px 18px;
    border-bottom:1px solid var(--border-soft);
    background:linear-gradient(180deg,#ffffff,#fffaf0);
  }

  .card-title{
    display:flex;
    align-items:center;
    gap:11px;
    min-width:0;
  }

  .card-title-icon{
    width:36px;
    height:36px;
    display:grid;
    place-items:center;
    border-radius:13px;
    background:#fff1b8;
    color:#6c4d00;
    border:1px solid #f0d476;
  }

  .card h2{
    margin:0;
    color:#382b05;
    font-size:14px;
    line-height:1.15;
    text-transform:uppercase;
    letter-spacing:.08em;
  }

  .card-subtitle{
    margin-top:3px;
    color:var(--muted);
    font-size:12px;
  }

  .card-body{
    padding:16px;
  }

  .toolbar{
    display:flex;
    align-items:center;
    gap:9px;
    flex-wrap:wrap;
  }

  .spacer{
    flex:1 1 auto;
  }

  .row{
    display:flex;
    align-items:center;
    gap:10px;
    min-height:60px;
    padding:12px;
    margin-bottom:10px;
    border:1px solid #f0dfaa;
    border-radius:16px;
    background:linear-gradient(180deg,#ffffff,#fffdf7);
    transition:transform .15s ease, border-color .15s ease, background .15s ease, box-shadow .15s ease;
  }

  .row:hover{
    transform:translateY(-1px);
    border-color:#e5bd37;
    background:#fffaf0;
    box-shadow:0 10px 26px rgba(217,154,0,.12);
  }

  .row:last-child{
    margin-bottom:0;
  }

  .job-row{
    cursor:pointer;
  }

  .job-row.selected{
    border-color:#d99a00;
    box-shadow:0 0 0 4px rgba(217,154,0,.16);
    background:#fff4d1;
  }

  .name{
    color:#2f2508;
    font-weight:850;
    letter-spacing:-.01em;
  }

  .account-main,
  .job-main{
    display:flex;
    flex-direction:column;
    min-width:0;
  }

  .account-meta,
  .job-meta{
    margin-top:2px;
    color:var(--muted);
    font-size:12px;
  }

  .plat{
    display:flex;
    align-items:center;
    gap:8px;
    margin:18px 0 10px;
    color:#7a5600;
    font-size:12px;
    font-weight:900;
    text-transform:uppercase;
    letter-spacing:.12em;
  }

  .plat:first-child{
    margin-top:0;
  }

  .plat::before{
    content:"";
    width:8px;
    height:8px;
    border-radius:999px;
    background:linear-gradient(135deg,#d99a00,#f5b800);
    box-shadow:0 0 0 4px rgba(245,184,0,.16);
  }

  button{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    gap:7px;
    min-height:37px;
    padding:8px 14px;
    border:0;
    border-radius:13px;
    color:#2d2100;
    font-weight:850;
    background:linear-gradient(135deg,#ffd84d,#f5b800);
    box-shadow:0 10px 24px rgba(217,154,0,.20);
    transition:transform .15s ease, opacity .15s ease, background .15s ease, box-shadow .15s ease;
  }

  button:hover{
    transform:translateY(-1px);
    background:linear-gradient(135deg,#ffcf24,#e9a900);
    box-shadow:0 14px 30px rgba(217,154,0,.26);
  }

  button:active{
    transform:translateY(0);
  }

  button:disabled{
    cursor:not-allowed;
    color:#9ca3af;
    background:#f3f4f6;
    box-shadow:none;
    opacity:.8;
  }

  button.ghost{
    color:#5d4300;
    background:#ffffff;
    border:1px solid #eed68e;
    box-shadow:0 8px 20px rgba(134,94,0,.07);
  }

  button.ghost:hover{
    background:#fff8df;
    border-color:#e4bd3a;
  }

  .badge{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    gap:6px;
    min-height:26px;
    padding:5px 10px;
    border-radius:999px;
    font-size:11px;
    font-weight:900;
    line-height:1;
    text-transform:uppercase;
    letter-spacing:.06em;
    white-space:nowrap;
    border:1px solid transparent;
  }

  .badge::before{
    content:"";
    width:6px;
    height:6px;
    border-radius:999px;
    background:currentColor;
    box-shadow:0 0 12px currentColor;
  }

  .b-green{
    color:var(--green);
    background:var(--green-bg);
    border-color:#b8edca;
  }

  .b-amber{
    color:#a66a00;
    background:var(--amber-bg);
    border-color:#f3d88b;
  }

  .b-red{
    color:var(--red);
    background:var(--red-bg);
    border-color:#f6c6c2;
  }

  .b-purple{
    color:var(--purple);
    background:var(--purple-bg);
    border-color:#dcc8ff;
  }

  .b-gray{
    color:var(--gray);
    background:var(--gray-bg);
    border-color:#e5e7eb;
  }

  #notice{
    min-height:22px;
    margin:0 0 14px;
    padding:0 2px;
    color:var(--muted);
    font-size:13px;
  }

  #notice.err{
    color:#d93025;
    font-weight:700;
  }

  #notice.ok{
    color:#18864b;
    font-weight:700;
  }

  .logs-card{
    margin-top:18px;
  }

  .log-shell{
    position:relative;
  }

  pre{
    min-height:360px;
    max-height:520px;
    overflow:auto;
    margin:0;
    padding:16px;
    border:1px solid #f0dfaa;
    border-radius:16px;
    color:#3f3004;
    background:
      linear-gradient(180deg, rgba(255,248,223,.95), rgba(255,255,255,.96)),
      #ffffff;
    font-size:12.8px;
    line-height:1.62;
    white-space:pre-wrap;
    word-break:break-word;
    tab-size:2;
  }

  pre::-webkit-scrollbar{
    width:10px;
    height:10px;
  }

  pre::-webkit-scrollbar-thumb{
    border-radius:999px;
    background:#e7c85e;
  }

  pre::-webkit-scrollbar-track{
    background:#fff8df;
  }

  .mono{
    font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas,
                "Liberation Mono", "Courier New", monospace;
  }

  .small{
    color:var(--muted);
    font-size:12px;
  }

  .empty{
    padding:18px;
    border:1px dashed #e8ce7c;
    border-radius:16px;
    color:var(--muted);
    background:#fffaf0;
    text-align:center;
  }

  @media(max-width:1060px){
    .stats{
      grid-template-columns:repeat(2,minmax(0,1fr));
    }
  }

  @media(max-width:900px){
    .app{
      display:block;
    }

    .sidebar{
      display:none;
    }

    .main{
      padding:18px;
    }

    .mobile-brand{
      display:flex;
      align-items:center;
      gap:12px;
      margin-bottom:16px;
    }

    .topbar{
      align-items:flex-start;
      flex-direction:column;
    }

    .hero-content{
      align-items:flex-start;
      flex-direction:column;
    }

    .grid{
      grid-template-columns:1fr;
    }
  }

  @media(max-width:560px){
    .main{
      padding:14px;
    }

    .stats{
      grid-template-columns:1fr;
    }

    .page-title h1{
      font-size:24px;
    }

    .hero{
      padding:18px;
    }

    .card-head{
      align-items:flex-start;
      flex-direction:column;
    }

    .row{
      align-items:flex-start;
      flex-direction:column;
    }

    .spacer{
      display:none;
    }

    button{
      width:100%;
    }

    .toolbar button{
      width:auto;
    }
  }
</style>
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

<script>
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
</script>
</body>
</html>"""


# 回傳 dashboard 的 route 區塊
# 此區塊提供系統的網頁介面。

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML
