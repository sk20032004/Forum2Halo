"""离线测试：抓一个帖子并输出解析结果（不上传 Halo）。

用法：
    .venv/Scripts/python.exe test_parse.py "https://www.javbus.com/forum/forum.php?mod=viewthread&tid=177470" --proxy http://127.0.0.1:7897
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

from importer import Settings
from importer import Importer


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--proxy", default=os.environ.get("FORUM_PROXY", ""))
    ap.add_argument("--out", default="work/preview")
    args = ap.parse_args()

    st = Settings(proxy=args.proxy)
    st.referer = ""
    imp = Importer(st, log=lambda m: print(m, flush=True))

    t0 = time.time()
    data, raw = imp.fetch_thread(args.url, save_dir=args.out)
    print(f"\n用时 {time.time() - t0:.1f}s")
    print("=" * 70)
    print("标题:", data.title)
    print("作者:", data.author, "| 时间:", data.posted_at, "| 版块:", data.forum_name)
    print("标签:", data.tags)
    print("图片数:", len(data.images))
    print("正文 HTML 长度:", len(data.content_html))
    plain = re.sub(r"<[^>]+>", "", data.content_html)
    print("纯文本长度:", len(plain))
    print("=" * 70)
    print("正文 HTML 前 1200 字：\n")
    print(data.content_html[:1200])
    print("\n正文 HTML 后 600 字：\n")
    print(data.content_html[-600:])
    print("\n图片列表：")
    for im in data.images:
        print(" ", im.order, im.src)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "content.html"), "w", encoding="utf-8") as f:
        f.write(data.content_html)
    with open(os.path.join(args.out, "content.txt"), "w", encoding="utf-8") as f:
        f.write(re.sub(r"<br\s*/?>", "\n", data.content_html))
    print(f"\n已保存到 {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
