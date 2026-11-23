from playwright.sync_api import sync_playwright
from accounts import get_account
from net_utils import goto_with_retry
import sys
import random
import os
import json
import time


# ============================================================
# CONFIG
# ============================================================

# 目標 TikTok 連結。
# 可以是 profile：
# TARGET_TIKTOK_URL = "https://www.tiktok.com/@username"
#
# 或是特定影片連結：
# TARGET_TIKTOK_URL = "https://www.tiktok.com/@username/video/1234567890"
TARGET_TIKTOK_URL = "https://www.tiktok.com/@danggiaanh18"

# 執行完畢後自動關閉瀏覽器。
# 若想保留視窗以便手動檢查，請設為 False。
AUTO_CLOSE = True

# 保留原本的瀏覽 + 按讚部分，此設定只開啟/關閉後續的附加流程。
ENABLE_TIKTOK_TARGET_FLOW = True

# 附加功能。
ENABLE_TIKTOK_FOLLOW = True
ENABLE_TIKTOK_COMMENT = True

# 進入指定頁面後要處理的影片數量。
TIKTOK_TARGET_VIDEO_COUNT = 8

# 範例留言清單。
TIKTOK_COMMENTS = [
    "這部影片太棒了。",
    "內容很有趣。",
    "謝謝你的分享。",
    "我看過這部影片了。",
]


# ============================================================
# JOB RESULT（從 dashboard/scheduler 執行時使用）
# 後端會傳入 JOB_RESULT_PATH；手動執行時沒有此變數則略過，腳本照原本方式執行。
# ============================================================

def write_job_result(result_path, ok, platform, account, completed_steps=None, failed_step=None, error=None):
    if not result_path:
        return

    data = {
        "ok": bool(ok),
        "status": "success" if ok else "failed",
        "platform": platform,
        "account": account,
        "completed_steps": list(completed_steps or []),
        "failed_step": failed_step,
        "error": error,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    try:
        os.makedirs(os.path.dirname(result_path) or ".", exist_ok=True)
        tmp = result_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, result_path)
    except Exception as e:
        print("[警告]", f"無法寫入 job result：{e}")


def _step(job, name):
    if job is not None:
        job["current_step"] = name


def _done(job, *names):
    if job is not None:
        job["completed_steps"].extend(names or [job["current_step"]])


def _finish(job, acc, ok, failed_step=None, error=None):
    write_job_result(
        job["result_path"], ok, "tiktok", acc["name"],
        completed_steps=job["completed_steps"],
        failed_step=failed_step, error=error,
    )
    job["written"] = True


# ============================================================
# LOGIN / SESSION
# ============================================================

def is_logged_in(context):
    return any(
        c["name"] == "sessionid" and c["value"]
        for c in context.cookies("https://tiktok.com")
    )


def wait_for_login(context, page, timeout_s=300):
    for _ in range(timeout_s // 2):
        if page.is_closed():
            return False

        try:
            if is_logged_in(context):
                return True

            page.wait_for_timeout(2000)

        except Exception:
            return False

    return False


def close_after_scroll(context, page, session_file, tag):
    # 儲存最新的 session 後關閉瀏覽器。
    # 若視窗已被手動關閉則回傳 False。
    if page.is_closed():
        return False

    try:
        context.storage_state(path=session_file)
        print(tag, "瀏覽完畢，已更新 session 並自動關閉瀏覽器。")
        context.close()
        return True

    except Exception as e:
        print(tag, f"無法自動關閉瀏覽器：{e}")
        return False


def keep_open_until_closed(context, tag):
    # 從 dashboard/scheduler 執行（有 JOB_RESULT_PATH）時沒有人會關閉視窗
    # （伺服器使用 xvfb）-> 直接關閉讓行程結束，避免永久卡住。
    if os.environ.get("JOB_RESULT_PATH"):
        print(tag, "由後端執行中：直接關閉瀏覽器，而非等待手動關閉。")
        try:
            context.close()
        except Exception:
            pass
        return

    print(tag, "瀏覽器已開啟。想結束時請自行關閉視窗...")

    try:
        while any(not pg.is_closed() for pg in context.pages):
            context.pages[0].wait_for_timeout(1000)

    except Exception:
        pass


# =========================================================================
# 舊區塊：自動瀏覽 TIKTOK + 按讚的函式
# 此部分邏輯維持你目前的程式碼不變。
# =========================================================================

def auto_scroll_tiktok_videos(page, total_videos=15):
    print("[系統] 開始自動觀看 TikTok 影片的測試流程...")

    # 影片區塊的總 selector，用於檢查頁面載入狀態
    video_selector = '[data-e2e="recommend-list-item-container"]'

    try:
        page.wait_for_selector(video_selector, timeout=10000)
    except Exception:
        print("[ 警告 ] 影片介面載入稍慢，開始強制執行...")

    for i in range(total_videos):
        if page.is_closed():
            break

        print(f"\n--- 正在測試第 {i + 1}/{total_videos} 部影片 ---")

        # 1. 模擬在隨機時間長度內觀看影片的行為
        watch_time = random.randint(6000, 12000)
        print(f"[*] 正在停留畫面以載入影片資料並檢查 UI：{watch_time / 1000} 秒...")
        page.wait_for_timeout(watch_time)

        # 2. 處理隨機按讚互動（機率 25% - 平均每瀏覽 4 部按讚 1 部）
        if random.random() < 0.25:
            try:
                # 依 TikTok 的 data-e2e 屬性直接掃描畫面上顯示的讚按鈕
                # 使用篩選器取得第一個實際顯示的按鈕 (.filter(visible=True).first)
                like_icon = page.locator('[data-e2e="like-icon"]').filter(has_text="").filter(visible=True).first

                if like_icon.is_visible():
                    # 透過檢查內部 SVG 的顏色（紅色）判斷此按鈕是否已被點擊
                    svg_element = like_icon.locator('svg').first
                    is_already_liked = False

                    if svg_element.is_visible():
                        svg_class = svg_element.get_attribute("class") or ""
                        # 按讚後，SVG 的 class 或 fill 屬性通常會變成特定的顏色狀態
                        if "active" in svg_class.lower() or "color" in svg_class.lower():
                            is_already_liked = True

                    if not is_already_liked:
                        print("[互動] 命中隨機機率，開始對影片按讚...")
                        page.wait_for_timeout(random.randint(1000, 2000))  # 短暫停頓以模擬手部反應

                        # Click force=True 穿透影片播放器的保護層
                        like_icon.click(force=True)
                        print("[成功] 已自動對 TikTok 影片按讚成功！")

                        page.wait_for_timeout(random.randint(1500, 2500))
                    else:
                        print("[略過] 系統記錄你先前已對此影片按讚。")
                else:
                    print("[警告] 在目前介面上找不到顯示中的讚按鈕。")

            except Exception as e:
                print(f"[處理錯誤] 無法操作讚按鈕：{str(e)}")

        # 3. 像真實使用者一樣按下 ArrowDown 鍵切換影片
        print("[*] 按下向下方向鍵切換到下一部影片...")
        page.keyboard.press("ArrowDown")

        # 等待 TikTok 的轉場動畫穩定
        page.wait_for_timeout(random.randint(2500, 4000))

        # 4. 檢查 DOM 載入以避免 flaky
        try:
            current_loaded_cards = page.locator(video_selector).count()
            js_condition = f"document.querySelectorAll('{video_selector}').length >= {current_loaded_cards}"
            page.wait_for_function(js_condition, timeout=4000)
            print(f"[ 確認 ] 影片切換成功。已快取的影片區塊總數：{current_loaded_cards}")

        except Exception:
            pass

    print("[系統] 自動瀏覽 TikTok 影片流程結束。")


# =========================================================================
# 附加部分：進入指定頁面 + FOLLOW + COMMENT
# 不更動 auto_scroll_tiktok_videos()。
# =========================================================================

def close_tiktok_popups(page, tag):
    """
    若出現 TikTok 常見的 popup 則將其關閉。
    """
    selectors = [
        'button[aria-label="Close"]',
        'button[aria-label="close"]',
        'button[data-e2e="modal-close-inner-button"]',
        'div[role="button"][aria-label="Close"]',
        'button:has-text("Not now")',
        'button:has-text("Không phải bây giờ")',
        'div[role="button"]:has-text("Not now")',
        'div[role="button"]:has-text("Không phải bây giờ")',
    ]

    for selector in selectors:
        try:
            btn = page.locator(selector).first

            if btn.is_visible(timeout=1000):
                btn.click(timeout=2000)
                print(f"{tag} [TikTok] 已關閉 popup：{selector}")
                page.wait_for_timeout(1000)
                return True

        except Exception:
            continue

    return False


def go_to_tiktok_target(page, target_url, tag):
    """
    前往指定的 TikTok profile/影片。
    """
    print(f"{tag} [TikTok] 正在開啟目標頁面：{target_url}")

    try:
        goto_with_retry(page, target_url, tag)
        page.wait_for_timeout(6000)

        close_tiktok_popups(page, tag)

        print(f"{tag} [TikTok] redirect 後目前的 URL：{page.url}")
        return True

    except Exception as e:
        print(f"{tag} [TikTok] 開啟目標頁面時發生錯誤：{e}")
        return False


def is_tiktok_video_url(url):
    """
    檢查目前的連結是否為 TikTok 影片連結。
    """
    return "/video/" in str(url)


def open_first_video_on_profile(page, tag):
    """
    若 TARGET_TIKTOK_URL 是 profile，此函式會開啟第一部影片。
    若 TARGET_TIKTOK_URL 已經是影片則不需使用。
    """
    print(f"{tag} [TikTok] 正在嘗試開啟 profile 上的第一部影片...")

    selectors = [
        '[data-e2e="user-post-item"]',
        'div[data-e2e="user-post-item"]',
        'a[href*="/video/"]',
    ]

    for selector in selectors:
        try:
            item = page.locator(selector).first

            if item.is_visible(timeout=4000):
                item.click(timeout=5000)
                print(f"{tag} [TikTok] 已開啟 profile 上的第一部影片。")
                page.wait_for_timeout(5000)
                return True

        except Exception:
            continue

    print(f"{tag} [TikTok] 無法開啟 profile 上的第一部影片。")
    return False


def follow_tiktok_target(page, tag):
    """
    若 profile/影片上有 Follow 按鈕則點擊 Follow。
    """
    print(f"{tag} [TikTok] 正在檢查 Follow 按鈕...")

    selectors = [
        '[data-e2e="follow-button"]',
        'button:has-text("Follow")',
        'button:has-text("Theo dõi")',
        'button:has-text("追蹤")',
        'button:has-text("关注")',
    ]

    for selector in selectors:
        try:
            btn = page.locator(selector).first

            if not btn.is_visible(timeout=1500):
                continue

            text = ""

            try:
                text = btn.inner_text(timeout=1000).strip()
            except Exception:
                pass

            lowered = text.lower()

            if (
                "following" in lowered
                or "friends" in lowered
                or "đang theo dõi" in lowered
                or "bạn bè" in lowered
                or "追蹤中" in text
                or "已关注" in text
            ):
                print(f"{tag} [TikTok] 先前已 follow 或已是朋友。")
                return 0

            page.wait_for_timeout(random.randint(1200, 2400))
            btn.click(timeout=3000)

            print(f"{tag} [TikTok] 已點擊 Follow。")
            page.wait_for_timeout(random.randint(2000, 3500))
            return 1

        except Exception:
            continue

    print(f"{tag} [TikTok] 找不到合適的 Follow 按鈕。")
    return 0


def open_tiktok_comment_panel(page, tag):
    """
    開啟目前影片的留言區。
    """
    selectors = [
        '[data-e2e="comment-icon"]',
        'span[data-e2e="comment-icon"]',
        'button[aria-label*="comment"]',
        'button[aria-label*="Comment"]',
        'button[aria-label*="bình luận"]',
        'button[aria-label*="Bình luận"]',
    ]

    for selector in selectors:
        try:
            btn = page.locator(selector).filter(visible=True).first

            if btn.is_visible(timeout=1500):
                btn.click(force=True, timeout=3000)
                print(f"{tag} [TikTok] 已開啟留言區。")
                page.wait_for_timeout(random.randint(2000, 3500))
                return True

        except Exception:
            continue

    print(f"{tag} [TikTok] 找不到開啟留言的按鈕。")
    return False


def comment_current_tiktok_video(page, tag):
    """
    在目前的影片留言。
    """
    try:
        if not open_tiktok_comment_panel(page, tag):
            return 0

        text = random.choice(TIKTOK_COMMENTS)

        textbox_selectors = [
            '[data-e2e="comment-input"]',
            'div[contenteditable="true"]',
            'div[role="textbox"]',
            'textarea',
        ]

        target_box = None

        for selector in textbox_selectors:
            try:
                box = page.locator(selector).filter(visible=True).first

                if box.is_visible(timeout=2000):
                    target_box = box
                    break

            except Exception:
                continue

        if not target_box:
            print(f"{tag} [TikTok] 找不到留言輸入框。")
            return 0

        target_box.click(timeout=3000)
        page.wait_for_timeout(random.randint(700, 1300))

        page.keyboard.type(text, delay=random.randint(60, 130))
        page.wait_for_timeout(random.randint(1000, 1800))

        post_selectors = [
            '[data-e2e="comment-post"]',
            'button:has-text("Post")',
            'button:has-text("Đăng")',
            'button:has-text("Send")',
            'button:has-text("Gửi")',
        ]

        for selector in post_selectors:
            try:
                post_btn = page.locator(selector).filter(visible=True).first

                if post_btn.is_visible(timeout=1000):
                    post_btn.click(timeout=3000)
                    print(f"{tag} [TikTok] 已送出留言：{text!r}")
                    page.wait_for_timeout(random.randint(2500, 4000))
                    return 1

            except Exception:
                continue

        page.keyboard.press("Enter")
        print(f"{tag} [TikTok] 已嘗試用 Enter 送出留言：{text!r}")
        page.wait_for_timeout(random.randint(2500, 4000))
        return 1

    except Exception as e:
        print(f"{tag} [TikTok] 留言時發生錯誤：{e}")
        return 0


def save_fail_screenshot(page, tag, account_name, step_name):
    # 步驟失敗時擷取畫面，方便判斷原因；截圖失敗不影響原本的錯誤回報。
    try:
        os.makedirs("diagnostics", exist_ok=True)
        path = os.path.join("diagnostics", f"tiktok_fail_{account_name}_{step_name}_{int(time.time())}.png")
        page.screenshot(path=path, timeout=15000)
        print(f"{tag} [TikTok] 步驟 {step_name} 失敗，截圖已儲存至 {path}（URL：{page.url}）")
    except Exception as e:
        print(f"{tag} [TikTok] 步驟 {step_name} 失敗，且無法截圖：{e}")


def interact_tiktok_target(page, target_url, tag, account_name="unknown", job=None):
    """
    auto_scroll_tiktok_videos() 執行完畢後的附加流程：
    - 進入指定的 TikTok 連結
    - 若有則 Follow
    - 若是 profile 則開啟第一部影片
    - 對部分影片留言
    - 用 ArrowDown 切換影片

    回傳第一個出錯步驟的 (failed_step, error)，若正常則回傳 (None, None)。
    流程照舊執行，只額外記錄哪個步驟出錯。
    """
    print(f"{tag} [TikTok] 開始目標頁面的附加流程。")

    failure = (None, None)

    _step(job, "goto_target_profile")
    if not go_to_tiktok_target(page, target_url, tag):
        save_fail_screenshot(page, tag, account_name, "goto_target_profile")
        return "goto_target_profile", f"無法開啟目標頁面：{target_url}"
    _done(job)

    followed_count = 0
    commented_count = 0
    comment_attempts = 0

    if ENABLE_TIKTOK_FOLLOW:
        _step(job, "follow_target")
        followed_count += follow_tiktok_target(page, tag)
        _done(job)

    if not is_tiktok_video_url(page.url):
        _step(job, "open_first_video")
        if open_first_video_on_profile(page, tag):
            _done(job)
        else:
            save_fail_screenshot(page, tag, account_name, "open_first_video")
            print(f"{tag} [TikTok] 停止後續流程。")
            return "open_first_video", f"無法開啟 profile 上的第一部影片。URL：{page.url}"

    page.wait_for_timeout(3000)
    _step(job, "comment_video" if ENABLE_TIKTOK_COMMENT else "watch_target_videos")

    for step in range(TIKTOK_TARGET_VIDEO_COUNT):
        if page.is_closed():
            break

        print(f"{tag} [TikTok] 正在處理目標影片 {step + 1}/{TIKTOK_TARGET_VIDEO_COUNT}")

        watch_time = random.randint(5000, 10000)
        print(f"{tag} [TikTok] 觀看影片 {watch_time / 1000:.1f} 秒")
        page.wait_for_timeout(watch_time)

        if ENABLE_TIKTOK_COMMENT and step % 3 == 0:
            comment_attempts += 1
            commented_count += comment_current_tiktok_video(page, tag)

            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(1200)
            except Exception:
                pass

        try:
            print(f"{tag} [TikTok] 切換到下一部影片。")
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(random.randint(2500, 4500))

        except Exception as e:
            print(f"{tag} [TikTok] 無法切換到下一部影片：{e}")
            break

    print(
        f"{tag} [TikTok] 附加流程總結："
        f"Follow={followed_count}, "
        f"Comment={commented_count}"
    )

    if failure[0] is None and comment_attempts and not commented_count:
        failure = ("comment_video", f"嘗試留言 {comment_attempts} 次，但一次都沒有送出成功。")
    elif failure[0] is None:
        _done(job)

    return failure


# ============================================================
# LOGIN TIKTOK MAIN
# ============================================================

def login_tiktok(acc):
    # 包裝主要流程：若有 JOB_RESULT_PATH 則寫入 job result (ok/failed_step/error)。
    job = {
        "result_path": os.environ.get("JOB_RESULT_PATH"),
        "completed_steps": [],
        "current_step": "start",
        "written": False,
    }

    try:
        _login_tiktok_flow(acc, job)
    except Exception as e:
        _finish(job, acc, False, failed_step=job["current_step"], error=f"{type(e).__name__}: {e}")
        raise

    if not job["written"]:
        _finish(job, acc, False, failed_step=job["current_step"],
                error="自動化已結束，但無法確定結果。")


def _login_tiktok_flow(acc, job):
    tag = f"[tiktok/{acc['name']}]"
    session_file = acc["session_file"]
    fail_png = f"tiktok_fail_{acc['name']}.png"

    with sync_playwright() as p:
        _step(job, "open_browser")
        context = p.chromium.launch_persistent_context(
            acc["profile_dir"],
            headless=False,
            channel="chrome"
        )

        page = context.pages[0] if context.pages else context.new_page()
        _done(job)

        # 若帳號設定中有 cookie 則載入。
        _step(job, "load_session")
        if not is_logged_in(context) and acc.get("cookies"):
            context.add_cookies(acc["cookies"])
            print(tag, f"已從 .env 載入 {len(acc['cookies'])} 個 cookie")
        _done(job)

        _step(job, "check_login")
        goto_with_retry(page, "https://www.tiktok.com/", tag)
        page.wait_for_timeout(3000)

        if not is_logged_in(context):
            if acc.get("email") and acc.get("password"):
                goto_with_retry(page, "https://www.tiktok.com/login/phone-or-email/email", tag)

                try:
                    page.wait_for_selector('input[name="username"]', timeout=15000)
                    page.type('input[name="username"]', acc["email"], delay=80)
                    page.type('input[type="password"]', acc["password"], delay=80)
                    page.click('button[type="submit"]')
                    page.wait_for_timeout(5000)

                except Exception as e:
                    print(tag, f"填寫 TikTok 登入資訊時發生錯誤：{e}")

            else:
                print(tag, ".env 中尚無 email/密碼，請手動登入。")
                goto_with_retry(page, "https://www.tiktok.com/login", tag)

        if not is_logged_in(context):
            print(
                tag,
                "尚未能登入。請處理 captcha/驗證或在瀏覽器中手動登入"
                "（請勿關閉視窗）。完成後腳本會自動偵測..."
            )

            if not wait_for_login(context, page):
                if page.is_closed():
                    print(tag, "瀏覽器視窗在登入完成前已被關閉。")
                    _finish(job, acc, False, failed_step="check_login",
                            error="瀏覽器視窗在登入完成前被關閉。")

                else:
                    print(tag, "已過 5 分鐘仍未能登入。URL：", page.url)
                    page.screenshot(path=fail_png)
                    print(tag, "已將螢幕截圖儲存至", fail_png)
                    _finish(job, acc, False, failed_step="need_verification",
                            error=f"已過 5 分鐘仍未能登入（captcha/驗證）。URL：{page.url}")
                    keep_open_until_closed(context, tag)

                return

        if is_logged_in(context):
            context.storage_state(path=session_file)
            print(tag, "✅ TikTok 登入成功，已將 session 儲存至", session_file)
            _done(job)  # check_login

            page.wait_for_timeout(3000)

            # ============================================================
            # 舊部分：維持原本的瀏覽 + 按讚
            # ============================================================
            _step(job, "auto_scroll_videos")
            auto_scroll_tiktok_videos(page, total_videos=20)
            _done(job)

            # ============================================================
            # 新部分：只在瀏覽完畢後附加執行
            # ============================================================
            target_failed_step, target_error = None, None
            if ENABLE_TIKTOK_TARGET_FLOW:
                target_failed_step, target_error = interact_tiktok_target(
                    page,
                    TARGET_TIKTOK_URL,
                    tag,
                    account_name=acc["name"],
                    job=job,
                )

            _step(job, "save_session")
            if AUTO_CLOSE and close_after_scroll(context, page, session_file, tag):
                _done(job, "save_session", "close_browser")
                if target_failed_step:
                    _finish(job, acc, False, failed_step=target_failed_step, error=target_error)
                else:
                    _finish(job, acc, True)
                return

            if target_failed_step:
                _finish(job, acc, False, failed_step=target_failed_step, error=target_error)
            elif AUTO_CLOSE:
                _finish(job, acc, False, failed_step="close_browser",
                        error="無法自動儲存 session/關閉瀏覽器（視窗可能已被手動關閉）。")

        else:
            print(tag, "❌ TikTok 登入失敗")
            print(tag, "目前的 URL：", page.url)
            page.screenshot(path=fail_png)
            print(tag, "已將螢幕截圖儲存至", fail_png)
            _finish(job, acc, False, failed_step="check_login", error=f"TikTok 登入失敗。URL：{page.url}")

        keep_open_until_closed(context, tag)

        if not job["written"]:
            # AUTO_CLOSE=False：流程跑完後由使用者自行關閉視窗
            _done(job, "close_browser")
            _finish(job, acc, True)


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    login_tiktok(get_account("tiktok", name))
