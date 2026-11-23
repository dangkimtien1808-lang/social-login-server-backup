from playwright.sync_api import Error as PlaywrightError


def goto_with_retry(page, url, tag, attempts=3, delay_ms=5000, timeout=60000):
    # 開啟頁面，若發生網路錯誤（DNS 失效、timeout、連線中斷...）則重試數次
    for i in range(1, attempts + 1):
        try:
            return page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except PlaywrightError as e:
            if i == attempts or page.is_closed():
                raise
            reason = str(e).splitlines()[0]
            print(tag, f"開啟 {url} 失敗（第 {i}/{attempts} 次）：{reason}。{delay_ms // 1000} 秒後重試...")
            page.wait_for_timeout(delay_ms)
