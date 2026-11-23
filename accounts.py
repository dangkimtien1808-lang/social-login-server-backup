from dotenv import load_dotenv
import json
import os
import re

load_dotenv()

ACCOUNTS_FILE = "accounts.json"
ENV_PREFIX = {"facebook": "FB", "tiktok": "TIKTOK"}
COOKIE_DOMAIN = {"facebook": ".facebook.com", "tiktok": ".tiktok.com"}


def _parse_cookie_string(raw, domain):
    # "c_user=...; xs=...; datr=..." -> 給 Playwright 用的 cookie 清單
    cookies = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or not value:
            continue
        cookies.append({
            "name": name, "value": value,
            "domain": domain, "path": "/",
        })
    return cookies


def _classify_segment(seg, platform):
    # 辨識字串 "a|b|c|d" 中的某一段屬於哪種類型
    s = seg.strip()
    if not s:
        return None, None
    if "c_user=" in s or "sessionid=" in s or (";" in s and "=" in s):
        return "cookie", s
    if re.fullmatch(r"EAA[A-Za-z0-9]+", s):
        return "token", s
    if s.isdigit():
        return "uid", s
    return "other", s  # username 或 password


def _parse_credential(raw, platform):
    # 接收 .env 中的完整字串，回傳 (login_id, password, cookies, token)。
    # 支援：單純的 email/電話/uid，或 "uid|pass|cookie|token" 字串。
    out = {"login_id": None, "password": None, "cookies": [], "token": None}
    if not raw:
        return out

    if "|" in raw:
        uid = others = None
        others = []
        for seg in raw.split("|"):
            kind, val = _classify_segment(seg, platform)
            if kind == "cookie":
                out["cookies"] = _parse_cookie_string(val, COOKIE_DOMAIN[platform])
            elif kind == "token":
                out["token"] = val
            elif kind == "uid":
                uid = val
            elif kind == "other":
                others.append(val)
        # 第一個 "other" 段視為密碼（常見格式中的第 2 個位置）
        if others:
            out["password"] = others[0]
        out["login_id"] = uid or (others[0] if others else None)
    else:
        out["login_id"] = raw.strip()
    return out


def load_accounts(platform):
    # 帳號清單取自 accounts.json（不含密碼）。
    # 在 .env 中，每個帳號都有自己的前綴，例如 FB_ACC2_...：
    #   FB_ACC2_CRED     = 完整字串 "uid|密碼|cookie|token"  （優先順序最高）
    #   FB_ACC2_COOKIE   = 僅 cookie 字串 "c_user=...;xs=..."
    #   FB_ACC2_EMAIL    = email / 電話 / uid / username
    #   FB_ACC2_PASSWORD = 密碼
    with open(ACCOUNTS_FILE, encoding="utf-8-sig") as f:
        entries = json.load(f).get(platform, [])

    accounts = []
    for e in entries:
        name = e["name"]
        prefix = e.get("env_prefix", f"{ENV_PREFIX[platform]}_{name.upper()}")

        cred = _parse_credential(os.getenv(f"{prefix}_CRED"), platform)

        # 允許個別設定每個欄位，覆寫從 CRED 解析出的結果
        email = os.getenv(f"{prefix}_EMAIL") or cred["login_id"]
        password = os.getenv(f"{prefix}_PASSWORD") or cred["password"]
        cookie_raw = os.getenv(f"{prefix}_COOKIE")
        cookies = (_parse_cookie_string(cookie_raw, COOKIE_DOMAIN[platform])
                   if cookie_raw else cred["cookies"])

        accounts.append({
            "platform": platform,
            "name": name,
            "email": email,
            "password": password,
            "cookies": cookies,
            "token": cred["token"],
            "profile_dir": e.get("profile_dir", f"profiles/{platform}_{name}"),
            "session_file": e.get("session_file", f"sessions/{platform}_{name}.json"),
        })
    return accounts


def get_account(platform, name):
    for acc in load_accounts(platform):
        if acc["name"] == name:
            os.makedirs(os.path.dirname(acc["session_file"]) or ".", exist_ok=True)
            return acc
    raise SystemExit(f"在 {ACCOUNTS_FILE} 中找不到 {platform} 的帳號 '{name}'")
