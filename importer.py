"""导入流水线：论坛帖子 → 图片转存 Halo → 生成文章。

步骤：
  1. 抓帖子页（支持代理）
  2. 解析出标题/作者/正文/图片
  3. 逐张下载图片 → 上传到 Halo 附件库（带重试、并发、失败回退原链接）
  4. 把正文里的图片地址替换成 Halo 地址
  5. 创建文章（草稿或直接发布），附带来源链接便于查重
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

import requests

from fetcher import UA, ThreadData, pick_parser, unwrap_text
from halo_client import HaloClient, HaloError

MAX_IMAGE_BYTES = 200 * 1024 * 1024  # 单图上限，防止抓到奇怪的大文件


@dataclass
class Settings:
    # 抓取
    proxy: str = ""                       # 例：http://127.0.0.1:7897，留空则直连
    cookie: str = ""                      # 需要登录可见的帖子填论坛 Cookie
    referer: str = ""                     # 图片防盗链时使用，默认取帖子站点首页
    # Halo
    halo_url: str = "http://192.168.5.220:8556"
    halo_token: str = ""
    category: str = ""                    # 分类显示名，如 "论坛收藏"
    publish: bool = False                 # False = 存草稿（推荐先审后发）
    upload_group: str = "forum2halo"
    # 排版
    add_source_block: bool = True         # 正文末尾附加来源信息框
    tag_from_title: bool = True           # 从标题抽番号作标签
    max_image_mb: int = 50
    threads: int = 3
    retries: int = 3

    @classmethod
    def from_file(cls, path: str) -> "Settings":
        import json
        if not os.path.exists(path):
            return cls()
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: str) -> None:
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump({k: getattr(self, k) for k in self.__dataclass_fields__}, f,
                      ensure_ascii=False, indent=2)


@dataclass
class ImportResult:
    ok: bool = False
    message: str = ""
    title: str = ""
    post_name: str = ""
    post_url: str = ""
    images_total: int = 0
    images_ok: int = 0
    images_failed: list[str] = field(default_factory=list)
    skipped_duplicate: bool = False
    log: list[str] = field(default_factory=list)


class Importer:
    def __init__(self, settings: Settings, log: Callable[[str], None] | None = None):
        self.st = settings
        self._log = log or (lambda m: None)
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        with self._lock:
            self._log(msg)

    # ------------------------------------------------------------ 抓取
    def _session(self, for_images: bool = False) -> requests.Session:
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        if self.st.proxy:
            s.proxies = {"http": self.st.proxy, "https": self.st.proxy}
        if self.st.cookie:
            s.headers["Cookie"] = self.st.cookie
        return s

    def fetch_thread(self, url: str, save_dir: str | None = None) -> tuple[ThreadData, str]:
        s = self._session()
        self.log(f"抓取帖子：{url}")
        # 注意：javbus 论坛对游客返回 302 → member.php?mod=logging（登录页），
        # 但正文其实就在 302 的响应体里。所以这里不自动跳转，先拿响应体试解析。
        r = s.get(url, timeout=30, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location", "")
            body = r.text
            if "postmessage" in body:
                self.log("游客状态：站点返回登录跳转，但响应体内含帖子正文，直接解析")
                html_text = body
            elif self.st.cookie:
                self.log(f"缺少登录态，带上 Cookie 重试（跳转到 {loc}）")
                r2 = s.get(url, timeout=30, allow_redirects=True)
                html_text = r2.text
            else:
                raise RuntimeError(
                    "该帖子需要登录才能查看（站点跳转到了登录页）。"
                    "请在『设置』里填入论坛 Cookie，或改用允许游客浏览的帖子。"
                )
        else:
            if r.status_code >= 400:
                raise RuntimeError(f"帖子页返回 {r.status_code}，可能需要登录或该帖不可见")
            html_text = r.text

        r.encoding = r.apparent_encoding or "utf-8"
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
            with open(os.path.join(save_dir, "thread.html"), "w", encoding="utf-8") as f:
                f.write(html_text)
        parser = pick_parser(url)
        data = parser.parse(url, html_text)
        self.log(f"解析完成：{data.title}｜图片 {len(data.images)} 张｜作者 {data.author or '-'}")
        return data, html_text

    # ------------------------------------------------------------ 图片
    def _download(self, sess: requests.Session, img_url: str, dest_dir: str,
                  index: int, referer: str) -> str:
        """下载到本地，返回文件路径。已存在则跳过（断点续传）。"""
        ext = os.path.splitext(img_url.split("?")[0])[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".mp4"):
            ext = ".jpg"
        key = hashlib.md5(img_url.encode()).hexdigest()[:12]
        path = os.path.join(dest_dir, f"{index:03d}_{key}{ext}")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path

        last_err = None
        for attempt in range(1, self.st.retries + 1):
            try:
                headers = {"Referer": referer or img_url}
                with sess.get(img_url, headers=headers, timeout=60, stream=True) as r:
                    if r.status_code >= 400:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    ctype = r.headers.get("Content-Type", "")
                    total = 0
                    with open(path + ".part", "wb") as f:
                        for chunk in r.iter_content(64 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > min(self.st.max_image_mb * 1024 * 1024, MAX_IMAGE_BYTES):
                                raise RuntimeError("文件超过大小上限")
                            f.write(chunk)
                if total == 0:
                    raise RuntimeError("空文件")
                os.replace(path + ".part", path)
                return path
            except Exception as e:  # noqa: BLE001
                last_err = e
                for p in (path, path + ".part"):
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                if attempt < self.st.retries:
                    self.log(f"  重试 {attempt}/{self.st.retries - 1}：{img_url}（{e}）")
        raise RuntimeError(f"下载失败：{img_url}（{last_err}）")

    # ------------------------------------------------------------ 主流程
    def run(self, url: str, work_dir: str,
            progress: Callable[[dict], None] | None = None,
            skip_duplicate: bool = True) -> ImportResult:
        st = self.st
        res = ImportResult()

        def push(**kw):
            if progress:
                progress(kw)

        from halo_client import HaloConfig
        halo = HaloClient(HaloConfig(base_url=st.halo_url, token=st.halo_token))

        # 0. 校验 Halo
        self.log("检查 Halo 连接…")
        try:
            halo.ping()
        except Exception as e:  # noqa: BLE001
            res.message = f"Halo 连接失败：{e}"
            self.log(res.message)
            return res

        # 1. 抓帖
        data, _ = self.fetch_thread(url, save_dir=work_dir)

        # 2. 查重
        if skip_duplicate:
            try:
                exist = halo.find_post_by_source(url)
                if exist:
                    res.skipped_duplicate = True
                    res.ok = True
                    res.title = data.title
                    res.post_name = exist["metadata"]["name"]
                    res.message = "该帖子之前已导入过，已跳过（如需重新导入请勾选“允许重复导入”）"
                    self.log(res.message)
                    return res
            except Exception as e:  # noqa: BLE001
                self.log(f"查重跳过：{e}")

        # 3. 图片下载 + 上传
        img_dir = os.path.join(work_dir, "images")
        os.makedirs(img_dir, exist_ok=True)
        referer = st.referer or _origin(url)
        sess = self._session(for_images=True)
        mapping: dict[str, str] = {}
        res.images_total = len(data.images)
        done = 0

        def handle(idx_img):
            idx, im = idx_img
            local = self._download(sess, im.src, img_dir, idx, referer)
            att = halo.upload_attachment(local, filename=os.path.basename(local),
                                         group=st.upload_group)
            link = halo.attachment_permalink(att)
            if not link:
                raise RuntimeError("上传成功但未返回地址")
            return im.src, link

        if data.images:
            self.log(f"开始转存 {len(data.images)} 张图片（并发 {st.threads}）…")
        with ThreadPoolExecutor(max_workers=max(1, st.threads)) as pool:
            futures = {pool.submit(handle, (i, im)): im for i, im in enumerate(data.images)}
            for fut in as_completed(futures):
                im = futures[fut]
                done += 1
                try:
                    src, link = fut.result()
                    mapping[src] = link
                    res.images_ok += 1
                    self.log(f"  [{done}/{len(data.images)}] OK {os.path.basename(link)}")
                except Exception as e:  # noqa: BLE001
                    res.images_failed.append(f"{im.src} :: {e}")
                    self.log(f"  [{done}/{len(data.images)}] 失败 {im.src} —— {e}")
                push(phase="images", done=done, total=len(data.images))

        # 4. 重写正文
        body = data.content_html
        for src, link in mapping.items():
            body = body.replace(f'src="{src}"', f'src="{link}"')
        if st.add_source_block:
            body = body + self._source_block(data, url)
        if res.images_failed:
            self.log(f"注意：{len(res.images_failed)} 张图上传失败，正文中保留原链接")

        # 5. 标签 / 分类
        tag_names = list(data.tags) if st.tag_from_title else []
        tag_ids: list[str] = []
        for t in tag_names:
            try:
                tag_ids.append(halo.ensure_tag(t))
            except Exception as e:  # noqa: BLE001
                self.log(f"标签 {t} 创建失败：{e}")
        cat_ids: list[str] = []
        if st.category:
            try:
                cat_ids.append(halo.ensure_category(st.category))
            except Exception as e:  # noqa: BLE001
                self.log(f"分类 {st.category} 创建失败：{e}")

        # 6. 建文章
        excerpt = unwrap_text(body).replace("\n", " ")[:120]
        annotations = {
            "forum2halo/source-url": url,
            "forum2halo/source-site": _host(url),
            "forum2halo/author": data.author,
            "forum2halo/posted-at": data.posted_at,
            "forum2halo/imported-at": _now(),
        }
        self.log(f"创建文章（{'发布' if st.publish else '草稿'}）…")
        post = halo.create_post(
            title=data.title, html=body, publish=st.publish,
            categories=cat_ids, tags=tag_ids, annotations=annotations, excerpt=excerpt,
        )
        name = (post.get("metadata") or {}).get("name", "")
        res.ok = True
        res.title = data.title
        res.post_name = name
        res.post_url = f"{st.halo_url.rstrip('/')}/console/posts/{name}"
        res.message = "导入完成"
        self.log(f"完成：{data.title}")

        # 7. 落一份存档，便于排查
        try:
            with open(os.path.join(work_dir, "result.md"), "w", encoding="utf-8") as f:
                f.write(f"# {data.title}\n\n- 来源: {url}\n- 作者: {data.author}\n"
                        f"- 时间: {data.posted_at}\n- 图片: {res.images_ok}/{res.images_total}\n"
                        f"- 文章: {name}\n")
                if res.images_failed:
                    f.write("\n## 失败的图片\n" + "\n".join(f"- {x}" for x in res.images_failed))
        except OSError:
            pass
        return res

    # ------------------------------------------------------------ 排版辅助
    @staticmethod
    def _source_block(data: ThreadData, url: str) -> str:
        parts = [f'<strong>原标题</strong>：{data.title}']
        if data.author:
            parts.append(f'<strong>作者</strong>：{data.author}')
        if data.posted_at:
            parts.append(f'<strong>发帖时间</strong>：{data.posted_at}')
        if data.forum_name:
            parts.append(f'<strong>版块</strong>：{data.forum_name}')
        parts.append(f'<strong>原文</strong>：<a href="{url}" target="_blank" rel="noopener">点击查看原帖</a>')
        body = "<br />\n".join(parts)
        return (
            '\n<hr />\n'
            '<blockquote style="border-left:4px solid #9e9d24;padding:8px 14px;'
            'background:#fafafa;font-size:14px;color:#666">'
            f'<p style="margin:0">{body}</p></blockquote>\n'
        )


def _origin(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
