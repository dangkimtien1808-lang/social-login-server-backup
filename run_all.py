from accounts import load_accounts
import os
import subprocess
import sys
import threading
import time

# 平行開啟 accounts.json 中的所有帳號，每個帳號使用獨立的行程與 profile。
#   python run_all.py                  -> 所有 Facebook + TikTok 帳號
#   python run_all.py facebook         -> 僅 Facebook
#   python run_all.py tiktok acc2      -> 僅 TikTok 的 acc2 帳號

SCRIPTS = {"facebook": "test_fb_login.py", "tiktok": "test_tiktok_login.py"}
LAUNCH_DELAY_S = 5  # 每次開啟瀏覽器之間的間隔，避免負載過高

print_lock = threading.Lock()


def relay_output(proc, tag):
    # 讀取子行程的 log，在每行開頭加上帳號名稱以免混淆
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        if not line.startswith(tag):
            line = f"{tag} {line}"
        with print_lock:
            print(line, flush=True)


def main():
    # 當 log 被重新導向到檔案/pipe 時，Windows 預設使用舊編碼 (cp950...)，無法輸出非 ASCII 文字
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    platforms = [sys.argv[1]] if len(sys.argv) > 1 else list(SCRIPTS)
    only = set(sys.argv[2:])

    # 子行程逐行即時輸出（不緩衝），並使用 UTF-8 以支援中文
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")

    procs, threads = [], []
    for platform in platforms:
        for acc in load_accounts(platform):
            if only and acc["name"] not in only:
                continue
            tag = f"[{platform}/{acc['name']}]"
            print(f"開啟 {platform}/{acc['name']} ...")
            if procs:
                time.sleep(LAUNCH_DELAY_S)
            proc = subprocess.Popen(
                [sys.executable, SCRIPTS[platform], acc["name"]],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=env)
            t = threading.Thread(target=relay_output, args=(proc, tag), daemon=True)
            t.start()
            procs.append(proc)
            threads.append(t)

    # 上面的 env 已複製 os.environ，因此後端的 JOB_ID / JOB_RESULT_PATH 會繼續傳給子行程
    codes = [proc.wait() for proc in procs]
    for t in threads:
        t.join()

    # 若有子行程出錯則回傳非 0 的錯誤碼，讓後端知道 job 失敗
    worst = max((abs(c) for c in codes), default=0)
    if worst:
        sys.exit(worst)


if __name__ == "__main__":
    main()
