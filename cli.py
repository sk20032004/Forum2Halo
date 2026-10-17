"""命令行导入工具，便于做定时任务/批量脚本。

用法：
    # 导入单个帖子（存草稿）
    .venv/Scripts/python.exe cli.py "https://www.javbus.com/forum/forum.php?mod=viewthread&tid=177470"

    # 批量（文件里一行一个链接），直接发布
    .venv/Scripts/python.exe cli.py -f urls.txt --publish

    # 只解析不上传
    .venv/Scripts/python.exe cli.py --dry-run "https://..."
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from importer import Importer, Settings

BASE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    ap = argparse.ArgumentParser(description="论坛帖子 → Halo 博客")
    ap.add_argument("urls", nargs="*", help="帖子地址")
    ap.add_argument("-f", "--file", help="链接列表文件，一行一个")
    ap.add_argument("--publish", action="store_true", help="直接发布（默认存草稿）")
    ap.add_argument("--dry-run", action="store_true", help="只解析，不上传")
    ap.add_argument("--allow-duplicate", action="store_true", help="允许重复导入")
    ap.add_argument("--proxy", help="覆盖配置里的代理")
    ap.add_argument("--category", help="覆盖分类名")
    ap.add_argument("--config", default=os.path.join(BASE, "config.json"))
    args = ap.parse_args()

    urls = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            urls += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not urls:
        ap.print_help()
        return 1

    st = Settings.from_file(args.config)
    if args.proxy is not None:
        st.proxy = args.proxy
    if args.category is not None:
        st.category = args.category
    if args.publish:
        st.publish = True

    ok = fail = 0
    for i, url in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] {url}")
        imp = Importer(st, log=lambda m: print("  " + m, flush=True))
        if args.dry_run:
            try:
                data, _ = imp.fetch_thread(url)
                print(f"  → {data.title}｜{data.author}｜{data.posted_at}"
                      f"｜图片 {len(data.images)}｜标签 {data.tags}")
                ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {e}")
                fail += 1
            continue
        try:
            res = imp.run(url, os.path.join(BASE, "work", f"cli_{int(time.time())}"),
                          skip_duplicate=not args.allow_duplicate)
            if res.ok:
                print(f"  ✓ {res.message}｜图片 {res.images_ok}/{res.images_total}"
                      f"｜{res.post_url}")
                ok += 1
            else:
                print(f"  ✗ {res.message}")
                fail += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {e}")
            fail += 1

    print(f"\n完成：成功 {ok}，失败 {fail}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
