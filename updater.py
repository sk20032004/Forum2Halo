"""静默检查更新：启动时后台查 GitHub Releases，有新版才提示，其余情况一律安静。"""
from __future__ import annotations

import re
import threading

import requests

APP_VERSION = "1.0.0"
# 发布后可改；也支持环境变量覆盖（fork/自用场景）
DEFAULT_REPO = "sk20032004/Forum2Halo"


def _repo() -> str:
    import os
    return os.environ.get("F2H_REPO", DEFAULT_REPO)


def parse_version(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0, 0, 0)


def check_latest(repo: str, timeout: int = 8) -> dict | None:
    """返回 {'version','url','notes'} 或 None（无新版/网络失败）。绝不抛异常。"""
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "Forum2Halo-Updater"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        tag = (data.get("tag_name") or "").lstrip("vV")
        latest = parse_version(tag)
        current = parse_version(APP_VERSION)
        if latest <= current:
            return None
        # 找安装包资产，找不到就给 release 页
        url = (data.get("html_url") or
               f"https://github.com/{repo}/releases/latest")
        for a in data.get("assets") or []:
            n = (a.get("name") or "").lower()
            if n.endswith(".exe") and "setup" in n or n.endswith("-setup.exe"):
                url = a.get("browser_download_url") or url
                break
        return {"version": tag, "url": url, "notes": data.get("body") or ""}
    except Exception:  # noqa: BLE001 — 静默：任何失败都不打扰用户
        return None


def check_async(on_update) -> None:
    """后台线程检查，有新版时回调 on_update(info)，无则什么都不做。"""
    def work():
        info = check_latest(_repo())
        if info:
            try:
                on_update(info)
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=work, daemon=True).start()
