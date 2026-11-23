from playwright.sync_api import sync_playwright
from accounts import get_account
from net_utils import goto_with_retry
import json
import os
import sys
import random
import time

TARGET_PAGE_URL = "https://www.facebook.com/share/1MqD4NSFej/?mibextid=wwXIfr"

AUTO_CLOSE = True

ENABLE_COMMENT_SIMULATION = True
ENABLE_FRIEND_SIMULATION = True

SIMULATED_COMMENTS = [
    "這篇文章寫得真好。",
    "謝謝你的分享。",
    "內容很實用。",
    "我看過這篇了。",
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
    job["current_step"] = name


def _done(job, *names):
    job["completed_steps"].extend(names or [job["current_step"]])


def _finish(job, acc, ok, failed_step=None, error=None):
    write_job_result(
        job["result_path"], ok, "facebook", acc["name"],
        completed_steps=job["completed_steps"],
        failed_step=failed_step, error=error,
    )
    job["written"] = True


# ============================================================
# LOGIN / SESSION
# ============================================================

def is_logged_in(context):
    return any(
        c["name"] == "c_user" and c["value"]
        for c in context.cookies("https://facebook.com")
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
    if page.is_closed():
        return False

    try:
        context.storage_state(path=session_file)
        print(tag, "已更新 session 並自動關閉瀏覽器。")
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


def import_old_session(context, session_file):
    if not os.path.exists(session_file):
        return

    try:
        with open(session_file, encoding="utf-8-sig") as f:
            cookies = json.load(f).get("cookies", [])

        if cookies:
            context.add_cookies(cookies)
            print("[警告]", f"已從舊 session 載入 {len(cookies)} 個 cookie。")
    except Exception as e:
        print("[警告]", f"無法載入舊 session：{e}")


def import_cred_cookies(context, acc, tag):
    cookies = acc.get("cookies")

    if cookies:
        context.add_cookies(cookies)
        print(tag, f"已從 .env 載入 {len(cookies)} 個 cookie")


# ============================================================
# DIAGNOSTICS 此區塊用於在程式執行時記錄 log、儲存截圖與診斷資料。
# ============================================================

def ensure_diagnostics_dir():
    os.makedirs("diagnostics", exist_ok=True)


def save_diagnostics(data, filename_prefix, tag):
    try:
        ensure_diagnostics_dir()
        path = os.path.join(
            "diagnostics",
            f"{filename_prefix}_{int(time.time())}.json"
        )

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(tag, f"[Diagnostics] 已儲存：{path}")
        return path

    except Exception as e:
        print(tag, f"[Diagnostics] 無法儲存檔案：{e}")
        return None


def save_viewport_screenshot(page, tag, account_name, step):
    try:
        ensure_diagnostics_dir()
        path = os.path.join(
            "diagnostics",
            f"fb_scan_{account_name}_scroll_{step}_{int(time.time())}.png"
        )

        page.screenshot(path=path, full_page=False)
        print(tag, f"[Diagnostics] 已擷取 screenshot：{path}")
        return path

    except Exception as e:
        print(tag, f"[Diagnostics] 無法擷取 screenshot：{e}")
        return None


# ============================================================
# WARM UP REELS 帳號暖身 此區塊用於開啟 Facebook、捲動動態消息，並在進入目標頁面前做準備。
# ============================================================
def find_post_like_buttons(page, tag, account_name="unknown"):
    valid_buttons = []

    # 方法 1：依屬性 data-ad-rendering-role="like_button" 尋找（如 DevTools 截圖所示）
    # 並取得外層 role="button" 的父元素
    locator = page.locator('div[role="button"]:has([data-ad-rendering-role="like_button"])')

    # 方法 2（備用）：若沒有上述屬性，則尋找以 Thích / Like 開頭的 aria-label
    if locator.count() == 0:
        locator = page.locator(
            'div[role="button"][aria-label^="Thích"], '
            'div[role="button"][aria-label^="Like"], '
            'div[role="button"][aria-label^="Bày tỏ cảm xúc"]'
        )

    count = locator.count()

    for i in range(count):
        btn = locator.nth(i)
        try:
            if not btn.is_visible(timeout=150):
                continue

            # 若此貼文已按過讚則略過
            if btn.get_attribute("aria-pressed") == "true":
                continue

            # 排除留言的讚按鈕（留言的 aria-label 通常含有 "bình luận" 或 "comment"）
            aria = (btn.get_attribute("aria-label") or "").lower()
            if "bình luận" in aria or "comment" in aria:
                continue

            box = btn.bounding_box()
            # 只取位於貼文顯示區域內的按鈕（避開最上方的 header）
            if box and box['y'] > 120:
                valid_buttons.append(btn)

        except Exception:
            continue

    print(f"{tag} [Debug] 找到 {len(valid_buttons)} 個可點擊的「貼文」讚按鈕。")
    return valid_buttons


def warm_up_before_page(page, tag):
    total_scrolls = random.randint(8, 12)
    print(f"{tag} [暖身] 開始一般瀏覽動態消息貼文（{total_scrolls} 次）...")

    feed_url = "https://facebook.com"
    goto_with_retry(page, feed_url, tag)
    page.wait_for_timeout(4000)

    liked_posts = 0

    for i in range(total_scrolls):
        if page.is_closed():
            break

        # 若不小心進入 Reels/Watch 則離開
        if "/reel/" in page.url or "/watch" in page.url:
            page.keyboard.press("Escape")
            page.wait_for_timeout(1500)

        read_s = random.randint(4, 7)
        print(f"{tag} [暖身] 正在瀏覽貼文第 {i + 1}/{total_scrolls} 次（閱讀 {read_s} 秒）...")
        page.wait_for_timeout(read_s * 1000)

        # 80% 機率對貼文按讚
        if random.random() < 0.80:
            try:
                # 1. 依 Facebook 新的 DOM 屬性尋找正確的貼文讚按鈕
                buttons = page.locator('div[role="button"]:has([data-ad-rendering-role="like_button"])')

                # 備用：若貼文以傳統 aria-label 形式顯示
                if buttons.count() == 0:
                    buttons = page.locator(
                        'div[role="button"][aria-label^="Thích"], '
                        'div[role="button"][aria-label^="Like"], '
                        'div[role="button"][aria-label^="Bày tỏ cảm xúc"]'
                    )

                count = buttons.count()

                for idx in range(count):
                    btn = buttons.nth(idx)
                    if not btn.is_visible(timeout=150):
                        continue

                    # 若此貼文已按讚則略過
                    if btn.get_attribute("aria-pressed") == "true":
                        continue

                    # 排除留言 (comment) 的讚按鈕
                    aria = (btn.get_attribute("aria-label") or "").lower()
                    if "bình luận" in aria or "comment" in aria:
                        continue

                    box = btn.bounding_box()
                    if not box:
                        continue

                    # 只點擊顯示在畫面中間區域的按鈕
                    if 150 <= box['y'] <= 750:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2

                        # 將滑鼠移到按鈕中心並實際點擊
                        page.mouse.move(cx, cy, steps=5)
                        page.wait_for_timeout(random.randint(250, 500))
                        page.mouse.click(cx, cy, delay=100)
                        page.wait_for_timeout(1200)

                        # 確認 Facebook 已記錄按讚
                        if btn.get_attribute("aria-pressed") == "true":
                            liked_posts += 1
                            print(f"{tag} [暖身] -> 已實際對貼文按讚！（總計：{liked_posts}）")
                            page.wait_for_timeout(random.randint(1200, 2200))
                            break

            except Exception as e:
                print(f"{tag} [暖身 - 小錯誤] {e}")

        # 向下捲動頁面
        scroll_px = random.randint(600, 850)
        page.mouse.wheel(0, scroll_px)
        page.wait_for_timeout(random.randint(2000, 3500))

    print(f"{tag} [暖身] 動態消息瀏覽完成（已按讚 {liked_posts} 篇）。切換至目標連結...")





# ============================================================
# LIKE BUTTON AUTO-DIAGNOSTIC 尋找讚按鈕 此區塊用於自動分析 HTML 介面，找出可能是讚按鈕的元素。
# ============================================================

def find_facebook_like_buttons(page, tag, limit=30, save_file=False, account_name="unknown"):
    """
    自動掃描目前的 DOM，尋找類似貼文讚 (Like/Thích) 按鈕的 candidate。
    此函式「不會」點擊，只收集資料以測試介面。
    """

    print(tag, "[AutoFind] 開始自動尋找讚 (Like/Thích) 按鈕 candidate...")

    try:
        results = page.evaluate(
            """
            (maxLimit) => {
                function shortText(el, max = 220) {
                    try {
                        return (el.innerText || el.textContent || '')
                            .replace(/\\s+/g, ' ')
                            .trim()
                            .slice(0, max);
                    } catch (e) {
                        return '';
                    }
                }

                function rectOf(el) {
                    try {
                        const r = el.getBoundingClientRect();
                        return {
                            x: Math.round(r.x),
                            y: Math.round(r.y),
                            width: Math.round(r.width),
                            height: Math.round(r.height),
                            top: Math.round(r.top),
                            bottom: Math.round(r.bottom)
                        };
                    } catch (e) {
                        return null;
                    }
                }

                function isVisible(el) {
                    try {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);

                        return r.width > 0 &&
                               r.height > 0 &&
                               s.visibility !== 'hidden' &&
                               s.display !== 'none' &&
                               Number(s.opacity || 1) > 0;
                    } catch (e) {
                        return false;
                    }
                }

                function nearestButton(el) {
                    if (!el) return null;
                    return el.closest('[role="button"], button, a[role="button"]');
                }

                function nearestArticle(el) {
                    if (!el) return null;
                    return el.closest(
                        '[role="article"], article, [data-pagelet*="FeedUnit"], [data-pagelet*="ProfileTimeline"]'
                    );
                }

                function normalizeText(s) {
                    return (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                }

                function isExactLikeText(s) {
                    const t = normalizeText(s);
                    return (
                        t === 'like' ||
                        t === 'thích' ||
                        t === '讚' ||
                        t === '赞'
                    );
                }

                function containsLikeWord(s) {
                    const t = normalizeText(s);
                    return (
                        t.includes('like') ||
                        t.includes('thích') ||
                        t.includes('讚') ||
                        t.includes('赞')
                    );
                }

                function hasBadWords(s) {
                    const t = normalizeText(s);

                    const badWords = [
                        'comment',
                        'bình luận',
                        'reply',
                        'trả lời',
                        'phản hồi',
                        'share',
                        'chia sẻ',
                        'send',
                        'gửi',
                        'follow',
                        'theo dõi',
                        'message',
                        'nhắn',
                        'viết bình luận',
                        'write a comment',
                        'page',
                        'trang',
                        'ẩn hoặc báo cáo',
                        'hide or report',
                        'xem thêm',
                        'see more',
                        'unlike',
                        'bỏ thích'
                    ];

                    return badWords.some(w => t.includes(w));
                }

                function looksLikeCommentAction(el) {
                    try {
                        let node = el.parentElement;

                        for (let i = 0; i < 5 && node; i++) {
                            const txt = normalizeText(node.innerText || '');
                            const aria = normalizeText(node.getAttribute('aria-label') || '');

                            if (
                                txt.includes('phản hồi') ||
                                txt.includes('reply') ||
                                aria.includes('comment') ||
                                aria.includes('bình luận')
                            ) {
                                return true;
                            }

                            node = node.parentElement;
                        }

                        return false;

                    } catch (e) {
                        return false;
                    }
                }

                function looksLikePostActionBar(el) {
                    try {
                        let node = el.parentElement;

                        for (let i = 0; i < 7 && node; i++) {
                            const txt = normalizeText(node.innerText || '');

                            const hasLike = txt.includes('thích') || txt.includes('like');
                            const hasComment = txt.includes('bình luận') || txt.includes('comment');
                            const hasShare = txt.includes('chia sẻ') || txt.includes('share');

                            if (hasLike && (hasComment || hasShare)) {
                                return true;
                            }

                            node = node.parentElement;
                        }

                        return false;

                    } catch (e) {
                        return false;
                    }
                }

                function getCandidateInfo(el, source) {
                    const aria = el.getAttribute('aria-label') || '';
                    const title = el.getAttribute('title') || '';
                    const text = shortText(el, 260);
                    const combined = `${aria} ${title} ${text}`
                        .replace(/\\s+/g, ' ')
                        .trim();

                    const article = nearestArticle(el);
                    const articleText = article ? shortText(article, 500) : '';
                    const rect = rectOf(el);

                    const exactLike =
                        isExactLikeText(aria) ||
                        isExactLikeText(title) ||
                        isExactLikeText(text);

                    const maybeLike =
                        exactLike ||
                        containsLikeWord(aria) ||
                        containsLikeWord(title) ||
                        containsLikeWord(text);

                    const commentLike = looksLikeCommentAction(el);
                    const postActionBar = looksLikePostActionBar(el);
                    const bad = hasBadWords(combined);

                    let score = 0;

                    if (maybeLike) score += 5;
                    if (exactLike) score += 8;
                    if (postActionBar) score += 5;
                    if (articleText) score += 2;

                    if (rect && rect.width >= 45 && rect.height >= 24) score += 5;
                    if (rect && rect.height >= 28) score += 1;

                    if (commentLike) score -= 8;
                    if (bad && !exactLike) score -= 5;

                    return {
                        source,
                        tag: el.tagName,
                        role: el.getAttribute('role'),
                        aria,
                        title,
                        text,
                        combined,
                        pressed: el.getAttribute('aria-pressed'),
                        exactLike,
                        maybeLike,
                        bad,
                        commentLike,
                        postActionBar,
                        score,
                        rect,
                        inArticle: Boolean(article),
                        articleText,
                        html: el.outerHTML.slice(0, 1500)
                    };
                }

                const candidates = [];
                const seen = new Set();

                const buttonLikeEls = Array.from(document.querySelectorAll(
                    '[role="button"], button, a[role="button"]'
                ));

                for (const el of buttonLikeEls) {
                    if (!isVisible(el)) continue;

                    const info = getCandidateInfo(el, 'button-scan');

                    if (!info.maybeLike) continue;

                    const key = [
                        info.source,
                        info.aria,
                        info.title,
                        info.text,
                        info.rect ? `${info.rect.x},${info.rect.y},${info.rect.width},${info.rect.height}` : ''
                    ].join('|');

                    if (seen.has(key)) continue;

                    seen.add(key);
                    candidates.push(info);
                }

                const textEls = Array.from(document.querySelectorAll('span, div'))
                    .filter(el => isVisible(el))
                    .filter(el => {
                        const txt = shortText(el, 80);
                        return isExactLikeText(txt);
                    });

                for (const textEl of textEls) {
                    const btn = nearestButton(textEl);

                    if (!btn || !isVisible(btn)) continue;

                    const info = getCandidateInfo(btn, 'text-parent-scan');

                    const key = [
                        info.source,
                        info.aria,
                        info.title,
                        info.text,
                        info.rect ? `${info.rect.x},${info.rect.y},${info.rect.width},${info.rect.height}` : ''
                    ].join('|');

                    if (seen.has(key)) continue;

                    seen.add(key);
                    candidates.push(info);
                }

                candidates.sort((a, b) => b.score - a.score);

                return candidates.slice(0, maxLimit);
            }
            """,
            limit
        )

        print(tag, f"[AutoFind] 找到 {len(results)} 個讚 (Like/Thích) 按鈕 candidate。")

        for idx, item in enumerate(results[:limit], start=1):
            print(tag, "================ AUTO LIKE CANDIDATE ================")
            print(tag, f"#{idx}")
            print(tag, f"score         : {item.get('score')}")
            print(tag, f"source        : {item.get('source')}")
            print(tag, f"tag/role      : {item.get('tag')} / {item.get('role')}")
            print(tag, f"exactLike     : {item.get('exactLike')}")
            print(tag, f"maybeLike     : {item.get('maybeLike')}")
            print(tag, f"bad           : {item.get('bad')}")
            print(tag, f"commentLike   : {item.get('commentLike')}")
            print(tag, f"postActionBar : {item.get('postActionBar')}")
            print(tag, f"pressed       : {item.get('pressed')!r}")
            print(tag, f"aria          : {item.get('aria')!r}")
            print(tag, f"title         : {item.get('title')!r}")
            print(tag, f"text          : {item.get('text')!r}")
            print(tag, f"rect          : {item.get('rect')}")
            print(tag, f"articleText   : {(item.get('articleText') or '')[:300]!r}")
            print(tag, f"html          : {(item.get('html') or '')[:500]!r}")

        if save_file and results:
            save_diagnostics(results, f"fb_autofind_like_{account_name}", tag)

        return results

    except Exception as e:
        print(tag, f"[AutoFind] 掃描讚按鈕 DOM 時發生錯誤：{e}")
        return []


def get_locator_from_candidate(page, candidate):
    aria = candidate.get("aria") or ""
    text = candidate.get("text") or ""
    rect = candidate.get("rect") or {}

    selectors = []

    if aria:
        safe_aria = aria.replace('"', '\\"')
        selectors.extend([
            f'div[role="button"][aria-label="{safe_aria}"]',
            f'button[aria-label="{safe_aria}"]',
            f'a[role="button"][aria-label="{safe_aria}"]',
        ])

    clean_text = text.strip()

    if clean_text in ["Thích", "Like", "讚", "赞"]:
        safe_text = clean_text.replace('"', '\\"')
        selectors.extend([
            f'div[role="button"]:has-text("{safe_text}")',
            f'button:has-text("{safe_text}")',
            f'a[role="button"]:has-text("{safe_text}")',
        ])

    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = loc.count()

            if count <= 0:
                continue

            if rect and "y" in rect:
                best = None
                best_dist = 999999

                for i in range(min(count, 30)):
                    item = loc.nth(i)

                    if not item.is_visible(timeout=500):
                        continue

                    box = item.bounding_box()

                    if not box:
                        continue

                    dist = abs(box["y"] - rect["y"])

                    if dist < best_dist:
                        best = item
                        best_dist = dist

                if best:
                    return best

            return loc.first

        except Exception:
            continue

    return None

#simple_scroll_and_like() 這是用來測試捲動頁面與尋找讚按鈕的輔助函式。

import os
import time

def simple_scroll_and_like(page, target_url, tag, max_scrolls=10):
    os.makedirs("diagnostics", exist_ok=True)
    print(f"{tag} 正在開啟頁面：{target_url}")
    page.goto(target_url, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)

    # [DEBUG 1] 檢查是否被踢回登入頁面
    if page.locator('input[name="email"]').is_visible():
        err_img = f"diagnostics/err_login_{int(time.time())}.png"
        page.screenshot(path=err_img)
        print(f"{tag} [嚴重錯誤] Session 已被登出！已截圖：{err_img}")
        return

    liked_count = 0

    for step in range(max_scrolls):
        print(f"\n{tag} --- 第 {step + 1}/{max_scrolls} 次瀏覽 ---")

        # 依貼文讚按鈕的標準 aria-label 尋找
        buttons = page.locator(
            'div[role="button"][aria-label="Thích"], div[role="button"][aria-label="Like"]'
        )
        count = buttons.count()
        print(f"{tag} [Debug] 找到 {count} 個 aria-label='Thích'/'Like' 的元素。")

        # [DEBUG 2] 若找到 0 個按鈕 -> 截圖並檢查頁面上有哪些按鈕
        if count == 0:
            diag_img = f"diagnostics/zero_btn_step{step+1}_{int(time.time())}.png"
            page.screenshot(path=diag_img)
            # 試著掃描是否有按鈕包含 Thích/Like 文字，以找出原因
            fallback_count = page.locator('div[role="button"]:has-text("Thích"), div[role="button"]:has-text("Like")').count()
            print(f"{tag} [警告] 沒有任何按鈕符合 aria-label！（有 {fallback_count} 個按鈕包含該文字）。已截圖：{diag_img}")

        for i in range(count):
            btn = buttons.nth(i)
            try:
                if not btn.is_visible(timeout=300):
                    print(f"{tag}   -> 按鈕 #{i+1}：略過（未顯示在畫面上 / is_visible=False）")
                    continue

                pressed = btn.get_attribute("aria-pressed")
                if pressed == "true":
                    print(f"{tag}   -> 按鈕 #{i+1}：略過（此貼文已按讚 / aria-pressed=true）")
                    continue

                box = btn.bounding_box()
                if not box or box["width"] < 40:
                    w = round(box["width"], 1) if box else 0
                    h = round(box["height"], 1) if box else 0
                    print(f"{tag}   -> 按鈕 #{i+1}：略過（尺寸太小：{w}x{h}px - 疑似留言按鈕）")
                    continue

                # 捲動到按鈕位置
                btn.scroll_into_view_if_needed()
                page.wait_for_timeout(800)

                # [DEBUG 3] 點擊按鈕並再次檢查 Facebook 是否已將狀態改為已按讚
                btn.click(delay=120, timeout=3000)
                page.wait_for_timeout(1200)

                after_pressed = btn.get_attribute("aria-pressed")
                if after_pressed == "true":
                    liked_count += 1
                    print(f"{tag} [OK] 已成功對第 {liked_count} 篇貼文按讚！")
                else:
                    # 若點擊後 aria-pressed 仍未改變 -> 立即截圖！
                    fail_img = f"diagnostics/click_no_effect_step{step+1}_btn{i+1}_{int(time.time())}.png"
                    # 在出錯的按鈕周圍畫紅框，方便在截圖中辨識
                    btn.evaluate("el => el.style.border = '3px solid red'")
                    page.screenshot(path=fail_img)
                    print(f"{tag} [點擊錯誤] 已點擊按鈕 #{i+1}，但 Facebook「沒有」記錄按讚（可能被 popup 遮住或功能被封鎖）。截圖：{fail_img}")

            except Exception as e:
                err_img = f"diagnostics/exception_step{step+1}_btn{i+1}_{int(time.time())}.png"
                page.screenshot(path=err_img)
                # 只取錯誤訊息的第一行，讓 log 簡潔
                short_err = str(e).splitlines()[0]
                print(f"{tag} [程式錯誤/TIMEOUT] 按鈕 #{i+1} 發生錯誤：{short_err} | 截圖：{err_img}")
                continue

        # 向下瀏覽至下一段
        page.evaluate("window.scrollBy(0, 800);")
        page.wait_for_timeout(2500)

    print(f"\n{tag} [總結] 完成！實際按讚總數：{liked_count} 篇。")

#simulate_comment_ui() 此區塊用於處理留言介面：尋找留言框、輸入內容並送出。
#名稱雖為「simulation」，但目前的程式碼可能會真的送出留言，因為有：page.keyboard.press("Enter")

def simulate_comment_ui(page, tag, account_name="unknown"):
    """
    尋找目前顯示的留言框，點擊後輸入內容並按 Enter 送出。
    """
    try:
        comment_boxes = page.locator(
            'div[role="textbox"][aria-label*="Viết bình luận"], '
            'div[role="textbox"][aria-label*="Write a comment"]'
        )
        
        if comment_boxes.count() == 0:
            open_cmt_btn = page.locator(
                'div[role="button"][aria-label="Bình luận"], '
                'div[role="button"][aria-label="Leave a comment"]'
            ).first
            if open_cmt_btn.is_visible(timeout=1500):
                open_cmt_btn.click()
                page.wait_for_timeout(random.randint(1000, 2000))

        target_box = comment_boxes.first
        if target_box.is_visible(timeout=2000):
            target_box.scroll_into_view_if_needed()
            page.wait_for_timeout(random.randint(500, 1000))
            target_box.click()
            
            text = random.choice(SIMULATED_COMMENTS)
            page.keyboard.type(text, delay=random.randint(60, 130))
            page.wait_for_timeout(random.randint(800, 1500))
            
            page.keyboard.press("Enter")
            print(f"{tag} [成功] 已留言：'{text}'")
            page.wait_for_timeout(random.randint(2500, 4000))
            return 1
            
    except Exception as e:
        print(f"{tag} [留言錯誤] 無法送出留言：{e}")
        
    return 0

# ============================================================
# MAIN PAGE FLOW 這是程式進入目標頁面後的主要控制流程。
# ============================================================

def follow_and_like_page(page, target_url, tag, account_name="unknown"):
    print(f"{tag} [系統] 正在前往目標連結：{target_url}")

    try:
        goto_with_retry(page, target_url, tag)
        page.wait_for_timeout(8000)

        print(f"{tag} [系統] Facebook redirect 後的 URL：{page.url}")

        # 1. 檢查追蹤 (Theo dõi / Follow) Page 按鈕。
        try:
            follow_btn = page.locator(
                'div[role="button"][aria-label="Theo dõi"], '
                'div[role="button"][aria-label="Follow"], '
                'div[role="button"]:has-text("Theo dõi"), '
                'div[role="button"]:has-text("Follow")'
            ).first

            if follow_btn.is_visible(timeout=1500):
                btn_text = follow_btn.inner_text().strip()

                if "Đang theo dõi" not in btn_text and "Following" not in btn_text:
                    page.wait_for_timeout(random.randint(1500, 3000))

                    print(f"{tag} [Debug] 找到 Page/Profile 的追蹤按鈕：{btn_text!r}")
                    page.wait_for_timeout(2500)
                else:
                    print(f"{tag} [略過] 帳號先前已追蹤。")

        except Exception as e_follow:
            print(f"{tag} [警告] 檢查追蹤按鈕時發生錯誤：{str(e_follow)}")

        max_scrolls = 15
        liked_count = 0
        comment_sim_count = 0
        friend_sim_count = 0
        if ENABLE_FRIEND_SIMULATION:
            try:
                add_btn = page.locator(
                    'div[role="button"][aria-label="Thêm bạn bè"], '
                    'div[role="button"][aria-label="Add friend"], '
                    'div[role="button"]:has-text("Thêm bạn bè"), '
                    'div[role="button"]:has-text("Add friend")'
                ).first
                if add_btn.is_visible(timeout=2000):
                    box = add_btn.bounding_box()
                    if box and box["y"] < 550:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        page.mouse.move(cx, cy, steps=5)
                        page.wait_for_timeout(400)
                        page.mouse.click(cx, cy, delay=100)
                        friend_sim_count += 1
                        print(f"{tag} [成功] 已送出交友邀請！")
                        page.wait_for_timeout(1500)
            except Exception as e_fr:
                print(f"{tag} [交友錯誤] {e_fr}")

        no_new_content_count = 0

        try:
            last_height = page.evaluate("document.body.scrollHeight")
        except Exception:
            last_height = 0

        print(f"{tag} [系統] 開始掃描貼文 / 留言 / 交友...")

        for step in range(max_scrolls):
            print(f"{tag} [系統] 正在掃描第 {step + 1}/{max_scrolls} 次捲動...")

            save_viewport_screenshot(page, tag, account_name, step + 1)

            post_like_buttons = find_post_like_buttons(
                page,
                tag,
                account_name=account_name
            )

            for btn in post_like_buttons:
                try:
                    btn.scroll_into_view_if_needed(timeout=3000)
                    page.wait_for_timeout(random.randint(700, 1200))

                    if not btn.is_visible(timeout=1000):
                        continue

                    if btn.get_attribute("aria-pressed") == "true":
                        continue

                    box = btn.bounding_box()

                    if not box:
                        continue

                    print(
                        f"{tag} [互動] 找到貼文讚 candidate "
                        f"第 {liked_count + 1} 個，y={int(box['y'])}，h={int(box['height'])}"
                    )

                    try:
                        btn.hover(timeout=2000)
                        page.wait_for_timeout(random.randint(300, 700))
                    except Exception:
                        pass

                    # 不自動在真實 Facebook 上按讚。
                                        # 不自動在真實 Facebook 上按讚。
                    btn.click(timeout=3000)

                    liked_count += 1

                    print(
                        f"{tag} [Debug] 已處理貼文讚 candidate。"
                        f"已處理的讚 candidate 總數：{liked_count}。"
                    )

                    page.wait_for_timeout(random.randint(1500, 2500))
                    break

                except Exception as e_btn:
                    print(f"{tag} [Debug] 處理讚 candidate 時發生錯誤：{e_btn}")
                    continue

            if ENABLE_COMMENT_SIMULATION and step % 3 == 0:
                try:
                    comment_sim_count += simulate_comment_ui(
                        page,
                        tag,
                        account_name=account_name
                    )
                except Exception as e_comment:
                    print(f"{tag} [CommentSim] 模擬留言時發生外部錯誤：{e_comment}")

            if ENABLE_FRIEND_SIMULATION and step % 5 == 0:
                try:
                    friend_sim_count += simulate_friend_ui(
                        page,
                        tag,
                        account_name=account_name
                    )
                except Exception as e_friend:
                    print(f"{tag} [FriendSim] 模擬交友時發生外部錯誤：{e_friend}")

            try:
                page.evaluate("window.scrollBy(0, 900);")
            except Exception:
                pass

            page.wait_for_timeout(random.randint(1800, 2600))

            try:
                new_height = page.evaluate("document.body.scrollHeight")
            except Exception:
                new_height = last_height

            if new_height == last_height:
                no_new_content_count += 1

                if no_new_content_count >= 3:
                    print(f"{tag} [系統] 已捲動多次但沒有新內容，停止。")
                    break
            else:
                no_new_content_count = 0
                last_height = new_height

        print(
            f"{tag} [總結] 完成！"
            f"已處理的讚 candidate：{liked_count}。"
            f"模擬留言：{comment_sim_count}。"
            f"模擬交友：{friend_sim_count}。"
            f"未實際送出 Like/Comment/Friend。"
        )

    except Exception as e:
        print(f"{tag} [錯誤] 與目標連結互動時發生問題：{str(e)}")


# ============================================================
# LOGIN FACEBOOK MAIN
# ============================================================

def login_facebook(acc):
    # 包裝主要流程：若有 JOB_RESULT_PATH 則寫入 job result (ok/failed_step/error)。
    job = {
        "result_path": os.environ.get("JOB_RESULT_PATH"),
        "completed_steps": [],
        "current_step": "start",
        "written": False,
    }

    try:
        _login_facebook_flow(acc, job)
    except Exception as e:
        _finish(job, acc, False, failed_step=job["current_step"], error=f"{type(e).__name__}: {e}")
        raise

    if not job["written"]:
        _finish(job, acc, False, failed_step=job["current_step"],
                error="自動化已結束，但無法確定登入狀態。")


def _login_facebook_flow(acc, job):
    tag = f"[facebook/{acc['name']}]"
    session_file = acc["session_file"]
    fail_png = f"fb_fail_{acc['name']}.png"

    with sync_playwright() as p:
        _step(job, "open_browser")
        context = p.firefox.launch_persistent_context(
            acc["profile_dir"],
            headless=False
        )

        page = context.pages[0] if context.pages else context.new_page()
        _done(job)

        _step(job, "load_session")
        if not is_logged_in(context):
            import_cred_cookies(context, acc, tag)

        if not is_logged_in(context):
            import_old_session(context, session_file)
        _done(job)

        _step(job, "check_login")
        goto_with_retry(page, "https://facebook.com/", tag)
        page.wait_for_timeout(3000)

        if not is_logged_in(context):
            email, password = acc.get("email"), acc.get("password")

            if email and password:
                print(tag, "Session 已過期，使用 .env 中的帳號重新登入 ...")

                try:
                    page.fill('input[name="email"]', email)
                    page.fill('input[name="pass"]', password)
                    page.press('input[name="pass"]', "Enter")
                    page.wait_for_timeout(5000)
                except Exception as e:
                    print(tag, f"填寫登入資訊時發生錯誤：{e}")
            else:
                print(tag, ".env 中尚無 email/密碼，請手動登入。")

        if not is_logged_in(context):
            print(tag, "尚未能登入。請處理 captcha/驗證或手動登入...")

            if not wait_for_login(context, page):
                if page.is_closed():
                    print(tag, "瀏覽器視窗在登入完成前已被關閉。")
                    _finish(job, acc, False, failed_step="check_login",
                            error="瀏覽器視窗在登入完成前被關閉。")
                else:
                    print(tag, "已過 5 分鐘仍未能登入。URL：", page.url)
                    page.screenshot(path=fail_png)
                    _finish(job, acc, False, failed_step="need_verification",
                            error=f"已過 5 分鐘仍未能登入（captcha/2FA/驗證）。URL：{page.url}")
                    keep_open_until_closed(context, tag)

                return

        if "/checkpoint/" in page.url:
            print(tag, "帳號被 checkpoint 鎖定。URL：", page.url)
            page.screenshot(path=fail_png)
            _finish(job, acc, False, failed_step="need_verification",
                    error=f"帳號遭到 checkpoint。URL：{page.url}")
            keep_open_until_closed(context, tag)
            return

        elif is_logged_in(context):
            context.storage_state(path=session_file)
            print(tag, "Facebook 登入成功，已將 session 儲存至", session_file)
            _done(job)  # check_login
            page.wait_for_timeout(2000)

            _step(job, "run_actions")
            warm_up_before_page(page, tag)

            follow_and_like_page(
                page,
                TARGET_PAGE_URL,
                tag,
                account_name=acc["name"]
            )
            _done(job)

            _step(job, "save_session")
            if AUTO_CLOSE and close_after_scroll(context, page, session_file, tag):
                _done(job, "save_session", "close_browser")
                _finish(job, acc, True)
                return

            keep_open_until_closed(context, tag)

            if AUTO_CLOSE:
                # close_after_scroll 失敗（通常是視窗被手動關閉）-> 無法儲存最後的 session
                _finish(job, acc, False, failed_step="close_browser",
                        error="無法自動儲存 session/關閉瀏覽器（視窗可能已被手動關閉）。")
            else:
                _done(job, "close_browser")
                _finish(job, acc, True)

        else:
            print(tag, "無法確定登入狀態。")


# ============================================================
# ENTRYPOINT 這是程式的執行起點。檢查使用者是否已輸入帳號名稱，依傳入的名稱取得帳號，再呼叫 login_facebook() 開始執行程式。
# ============================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Cach dung: python test_fb_login.py <ten_acc>")
        sys.exit(1)

    account = get_account("facebook", sys.argv[1])
    login_facebook(account)