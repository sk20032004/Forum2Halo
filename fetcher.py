"""论坛帖子解析器。

目前实现 Discuz! 系（javbus 论坛 / 老司機論壇）解析，按 url 特征自动匹配。
新增论坛时，写一个继承 BaseParser 的类并 register() 即可。
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup, NavigableString, Tag

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 帖子里的装饰性小图（分割线、图标），不上传
DECORATIVE_RE = re.compile(
    r"(?:^|/)(?:static/image|template/[^/]+/images)/"
    r"|/common/(?:hrline|arw_|hot_|stamp|wifi|online_)"
    r"|/smiley/|/line/",
    re.I,
)


@dataclass
class ImageItem:
    src: str            # 原始地址
    order: int          # 在正文中的序号
    width: int | None = None
    height: int | None = None


@dataclass
class ThreadData:
    source_url: str
    tid: str
    title: str
    author: str = ""
    posted_at: str = ""
    forum_name: str = ""
    content_html: str = ""        # 已清洗的正文 HTML（图片仍是原始地址）
    images: list[ImageItem] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


class BaseParser:
    """解析器基类：抓取 + 解析分离，方便测试。"""

    name = "base"

    @classmethod
    def match(cls, url: str) -> bool:  # pragma: no cover - 由子类实现
        return False

    def parse(self, url: str, page_html: str) -> ThreadData:  # pragma: no cover
        raise NotImplementedError


class DiscuzParser(BaseParser):
    """Discuz! X 论坛帖子解析。

    结构参考: div#post_<pid> > table#pid<pid> > td.t_f[id=postmessage_<pid>]
    """

    name = "discuz"

    @classmethod
    def match(cls, url: str) -> bool:
        p = urlparse(url)
        return "forum.php" in p.path + "?" + p.query and "mod=viewthread" in (p.query or "")

    def parse(self, url: str, page_html: str) -> ThreadData:
        soup = BeautifulSoup(page_html, "lxml")
        tid = (parse_qs(urlparse(url).query).get("tid") or [""])[0]

        # ---- 标题 ----
        title_el = soup.select_one("#thread_subject")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            t = soup.find("title")
            title = re.sub(r"\s*-\s*[^-]+$", "", t.get_text(strip=True)) if t else "未命名帖子"

        # ---- 版块 ----
        forum_name = ""
        crumb = soup.select_one("#pt, .nthread_info h1.ts a, .bm.cl h1 a")
        if crumb:
            forum_name = crumb.get_text(strip=True).strip("[]")

        # ---- 正文（只看楼主首帖）----
        body_td = None
        for td in soup.select("td.t_f"):
            if td.get("id", "").startswith("postmessage_"):
                body_td = td
                break
        if body_td is None:  # 有些主题用 div
            body_td = soup.select_one("div.t_fsz .t_f")

        # ---- 作者 / 发帖时间 ----
        # 该主题把作者信息放在 .nthread_other > .authi，不在 plhin 里，先试帖子内，再全局兜底
        author, posted_at = "", ""
        authi = None
        plhin = body_td.find_parent(class_="plhin") if body_td else None
        if plhin:
            authi = plhin.select_one(".authi")
        if authi is None:
            authi = soup.select_one(".nthread_other .authi") or soup.select_one(".authi")
        if authi:
            a = authi.select_one("a.au, a.xw1, a[href*='mod=space']")
            author = a.get_text(strip=True) if a else ""
            for el in authi.select("span, em"):
                m = re.search(r"\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}(?::\d{2})?",
                              el.get_text(" ", strip=True))
                if m:
                    posted_at = m.group(0)
                    break

        if body_td is None:
            raise ValueError("未找到帖子正文，可能被隐藏/需要登录/需要回复可见")

        # ---- 版块：面包屑 #pt 里排除「当前帖」后的最后一项 ----
        pt = soup.select_one("#pt .z") or soup.select_one("#pt")
        if pt:
            links = [
                x.get_text(strip=True)
                for x in pt.select("a")
                if x.get_text(strip=True)
                and "viewthread" not in (x.get("href") or "")
                and "forumdisplay" in (x.get("href") or "")
            ]
            forum_name = links[-1] if links else ""

        images: list[ImageItem] = []
        cleaned = self._clean(soup, body_td, url, images)

        # ---- 标签：从标题里抽番号，如 EBWH-356 ----
        tags = []
        for m in re.finditer(r"\b([A-Z]{2,6})-(\d{3,5})\b", title.upper()):
            tag = f"{m.group(1)}-{m.group(2)}"
            if tag not in tags:
                tags.append(tag)

        return ThreadData(
            source_url=url,
            tid=tid,
            title=title,
            author=author,
            posted_at=posted_at,
            forum_name=forum_name,
            content_html=cleaned,
            images=images,
            tags=tags,
            meta={"parser": self.name},
        )

    # ---------------------------------------------------------------- 内部
    def _clean(self, soup: BeautifulSoup, td: Tag, base_url: str, images: list[ImageItem]) -> str:
        frag = BeautifulSoup(td.decode_contents(), "lxml")
        root = frag.body or frag

        # 1. 去掉编辑标记 / 脚本 / iframe
        for sel in ("i.pstatus", "script", "style", "iframe", "div.attach_nopermission",
                    ".pstatus", ".aimg_tip", ".tip", ".blockcode", "font[size]"):
            for el in root.select(sel):
                if el.name == "i" and "pstatus" in (el.get("class") or []):
                    el.decompose()

        # 2. 处理图片：只保留内容图，记录原图地址
        order = 0
        for img in list(root.find_all("img")):
            src = img.get("src") or img.get("file") or img.get("data-src") or ""
            if not src:
                img.decompose()
                continue
            abs_src = urljoin(base_url, src)
            if DECORATIVE_RE.search(abs_src):
                img.decompose()
                continue
            img["src"] = abs_src
            img.attrs = {"src": abs_src, "alt": img.get("alt") or ""}
            images.append(
                ImageItem(
                    src=abs_src,
                    order=order,
                    width=_int(img.get("width")),
                    height=_int(img.get("height")),
                )
            )
            order += 1

        # 3. 相对链接补全；站内 /CODE 形式的链接补成可读文本
        for a in root.find_all("a"):
            href = a.get("href") or ""
            if not href or href.startswith(("javascript:", "#")):
                a.unwrap()
                continue
            a["href"] = urljoin(base_url, href)
            a.attrs = {"href": a["href"], "target": "_blank", "rel": "noopener"}

        # 4. font/span 内联标签：保留颜色语义（Discuz 会嵌 <font>，直接改属性最稳）
        for f in root.find_all("font"):
            if f.get("color"):
                f["style"] = f"color:{f['color']}"
            if f.get("size"):
                f.decompose()
                continue
            f.name = "span"
            f.attrs.pop("color", None)

        # 5. <br> 分段：连续 br 压缩为两个
        html_str = str(root)
        html_str = re.sub(r"<br\s*/?>", "<br />", html_str, flags=re.I)
        html_str = re.sub(r"(?:<br />\s*){3,}", "<br /><br />", html_str)

        # 6. 去掉 lxml 包裹
        html_str = re.sub(r"^\s*<(?:html|body)[^>]*>|</(?:html|body)>\s*$", "", html_str, flags=re.I)
        html_str = re.sub(r"</?(?:tbody|thead)>", "", html_str, flags=re.I)
        # 表格式排版（Discuz 常见）去表格，只留内容
        html_str = re.sub(r"</?table[^>]*>|</?tr[^>]*>|</?td[^>]*>", "", html_str, flags=re.I)
        html_str = re.sub(r"<!--.*?-->", "", html_str, flags=re.S)
        return html_str.strip()

def _int(v) -> int | None:
    try:
        return int(re.sub(r"[^\d]", "", str(v)))
    except (TypeError, ValueError):
        return None


PARSERS: list[type[BaseParser]] = [DiscuzParser]


def pick_parser(url: str) -> BaseParser:
    for cls in PARSERS:
        if cls.match(url):
            return cls()
    raise ValueError(f"暂不支持该站点：{url}")


def unwrap_text(html: str) -> str:
    """HTML → 纯文本（用于生成摘要）。"""
    txt = BeautifulSoup(html, "lxml").get_text("\n")
    txt = _html.unescape(txt)
    txt = re.sub(r"\n{2,}", "\n", txt)
    return txt.strip()
