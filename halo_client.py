"""Halo 2.x API 客户端。

用到的接口：
  POST /apis/api.console.halo.run/v1alpha1/attachments/upload   multipart，返回 Attachment
  POST /apis/api.console.halo.run/v1alpha1/posts                创建文章（支持 publish=false 存草稿）
  GET  /apis/api.console.halo.run/v1alpha1/posts                列出文章（查重）
认证：个人令牌（pat_xxx），请求头 Authorization: Bearer pat_xxx
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import requests


class HaloError(RuntimeError):
    pass


@dataclass
class HaloConfig:
    base_url: str
    token: str
    verify_ssl: bool = False
    timeout: int = 60


class HaloClient:
    def __init__(self, cfg: HaloConfig):
        self.cfg = cfg
        self.base = cfg.base_url.rstrip("/")
        self.s = requests.Session()
        self.s.trust_env = False  # 内网地址，绝不能走系统代理
        self.s.headers.update(
            {
                "Authorization": f"Bearer {cfg.token}",
                "Accept": "application/json",
                "User-Agent": "forum2halo/1.0",
            }
        )
        self.s.verify = cfg.verify_ssl

    # ------------------------------------------------------------ 基础
    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _check(self, r: requests.Response, what: str) -> dict:
        if r.status_code >= 400:
            detail = r.text[:500]
            try:
                j = r.json()
                detail = j.get("detail") or j.get("message") or detail
            except Exception:
                pass
            if r.status_code in (401, 403):
                if r.status_code == 403:
                    raise HaloError(
                        f"{what} 被拒绝（403）：令牌有效但权限不足。"
                        "请到 Halo 后台「个人中心 → 个人令牌」重新创建令牌，"
                        "创建时务必勾选包含『附件管理』和『文章管理』的权限（或直接选完整权限角色）。"
                    )
                raise HaloError(f"{what} 认证失败（401）：请检查个人令牌是否正确。{detail}")
            raise HaloError(f"{what} 失败（{r.status_code}）：{detail}")
        if not r.content:
            return {}
        try:
            return r.json()
        except Exception:
            return {}

    # ------------------------------------------------------------ 连通性
    def ping(self) -> dict:
        """用当前令牌拉一篇文章，验证地址 + 令牌。"""
        r = self.s.get(self._url("/apis/api.console.halo.run/v1alpha1/posts"),
                       params={"page": 0, "size": 1}, timeout=self.cfg.timeout)
        data = self._check(r, "连接 Halo")
        return {"total": data.get("total", 0), "ok": True}

    def me(self) -> dict:
        r = self.s.get(self._url("/apis/api.console.halo.run/v1alpha1/users/-"), timeout=self.cfg.timeout)
        return self._check(r, "获取当前用户").get("user", {})

    # ------------------------------------------------------------ 附件
    def ensure_group(self, group: str) -> str | None:
        """确保附件分组存在；不存在则创建。失败返回 None（调用方退回不分组）。"""
        if not group:
            return None
        try:
            r = self.s.get(self._url("/apis/storage.halo.run/v1alpha1/groups"),
                           params={"page": 0, "size": 200}, timeout=self.cfg.timeout)
            data = self._check(r, "读取附件分组")
            for it in data.get("items", []):
                if it.get("metadata", {}).get("name") == group:
                    return group
            # 不存在 → 创建
            body = {
                "apiVersion": "storage.halo.run/v1alpha1",
                "kind": "Group",
                "metadata": {"name": group, "generateName": "attachment-group-"},
                "spec": {"displayName": group},
            }
            r2 = self.s.post(self._url("/apis/storage.halo.run/v1alpha1/groups"),
                             json=body, timeout=self.cfg.timeout)
            self._check(r2, f"创建附件分组 {group}")
            return group
        except Exception:  # noqa: BLE001 — 分组建不了就退回不分组，不阻塞导入
            return None

    def upload_attachment(self, file_path: str, filename: str | None = None,
                          policy: str | None = None, group: str = "",
                          progress: Callable[[int, int], None] | None = None) -> dict:
        """上传本地文件到 Halo 附件库，返回 Attachment（含 status.permalink）。"""
        filename = filename or os.path.basename(file_path)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        size = os.path.getsize(file_path)

        policy_name = policy
        if not policy_name:
            policy_name = self.default_policy()

        group_name = self.ensure_group(group) if group else None

        with open(file_path, "rb") as fh:
            files = {"file": (filename, fh, mime)}
            data = {"policyName": policy_name, "size": str(size)}
            if group_name:
                data["groupName"] = group_name
            if progress:
                progress(0, size)
            r = self.s.post(
                self._url("/apis/api.console.halo.run/v1alpha1/attachments/upload"),
                files=files, data=data, timeout=max(self.cfg.timeout, 300),
            )
            if progress:
                progress(size, size)
        return self._check(r, f"上传图片 {filename}")

    def default_policy(self) -> str:
        """取第一个可用的存储策略（本地存储策略 name 通常是 'default-policy'）。"""
        r = self.s.get(self._url("/apis/storage.halo.run/v1alpha1/policies"),
                       params={"page": 0, "size": 20}, timeout=self.cfg.timeout)
        data = self._check(r, "读取存储策略")
        items = data.get("items") or []
        if not items:
            raise HaloError("Halo 没有可用的存储策略，请先在后台『附件 → 存储策略』里配置")
        for it in items:
            name = it.get("metadata", {}).get("name", "")
            if name == "default-policy":
                return name
        return items[0]["metadata"]["name"]

    def attachment_permalink(self, att: dict) -> str:
        """从上传结果里拿可访问 URL（相对路径 → 绝对）。"""
        link = (att.get("status") or {}).get("permalink") or ""
        if not link:
            spec = att.get("spec") or {}
            link = spec.get("permalink") or ""
        if link.startswith("http"):
            return link
        return f"{self.base}/{link.lstrip('/')}" if link else ""

    # ------------------------------------------------------------ 文章
    def find_post_by_source(self, source_url: str) -> dict | None:
        """按 annotation 里的来源链接查重，避免重复导入。"""
        r = self.s.get(self._url("/apis/api.console.halo.run/v1alpha1/posts"),
                       params={"page": 0, "size": 1,
                               "labelSelector": "forum2halo/source"}, timeout=self.cfg.timeout)
        # labelSelector 只为缩小范围；真正比对靠 annotation，扫最近 N 篇
        for page in range(0, 5):
            r = self.s.get(self._url("/apis/api.console.halo.run/v1alpha1/posts"),
                           params={"page": page, "size": 50}, timeout=self.cfg.timeout)
            data = self._check(r, "查询已有文章")
            for it in data.get("items", []):
                ann = (it.get("metadata") or {}).get("annotations") or {}
                if ann.get("forum2halo/source-url") == source_url:
                    return it
            if not data.get("items") or len(data["items"]) < 50:
                break
        return None

    def create_post(self, *, title: str, html: str, publish: bool = False,
                    categories: list[str] | None = None, tags: list[str] | None = None,
                    annotations: dict | None = None, excerpt: str = "",
                    slug: str | None = None) -> dict:
        name = str(uuid.uuid4())
        payload = {
            "post": {
                "apiVersion": "content.halo.run/v1alpha1",
                "kind": "Post",
                "metadata": {
                    "name": name,
                    "annotations": {
                        "content.halo.run/preferred-editor": "default",
                        **(annotations or {}),
                    },
                },
                "spec": {
                    "title": title,
                    "slug": slug or self._slugify(title),
                    "template": "",
                    "cover": "",
                    "deleted": False,
                    "publish": publish,
                    "pinned": False,
                    "allowComment": True,
                    "visible": "PUBLIC",
                    "priority": 0,
                    "excerpt": {"autoGenerate": not bool(excerpt), "raw": excerpt},
                    "categories": categories or [],
                    "tags": tags or [],
                    "htmlMetas": [],
                },
            },
            "content": {"raw": html, "content": html, "rawType": "HTML"},
        }
        r = self.s.post(self._url("/apis/api.console.halo.run/v1alpha1/posts"),
                        json=payload, timeout=self.cfg.timeout)
        return self._check(r, "创建文章")

    def publish_post(self, name: str) -> dict:
        r = self.s.put(self._url(f"/apis/api.console.halo.run/v1alpha1/posts/{name}/publish"),
                       json={}, timeout=self.cfg.timeout)
        return self._check(r, "发布文章")

    def get_post(self, name: str) -> dict:
        r = self.s.get(self._url(f"/apis/api.console.halo.run/v1alpha1/posts/{name}"),
                       timeout=self.cfg.timeout)
        return self._check(r, "读取文章")

    # ------------------------------------------------------------ 分类 / 标签
    def list_categories(self) -> list[dict]:
        r = self.s.get(self._url("/apis/api.console.halo.run/v1alpha1/categories"),
                       params={"page": 0, "size": 200}, timeout=self.cfg.timeout)
        return self._check(r, "读取分类").get("items", [])

    def list_tags(self) -> list[dict]:
        r = self.s.get(self._url("/apis/api.console.halo.run/v1alpha1/tags"),
                       params={"page": 0, "size": 200}, timeout=self.cfg.timeout)
        return self._check(r, "读取标签").get("items", [])

    def ensure_tag(self, display_name: str) -> str:
        """不存在则创建标签，返回 metadata.name。"""
        for t in self.list_tags():
            if (t.get("spec") or {}).get("displayName") == display_name:
                return t["metadata"]["name"]
        name = str(uuid.uuid4())
        body = {
            "apiVersion": "content.halo.run/v1alpha1",
            "kind": "Tag",
            "metadata": {"name": name, "generateName": "tag-"},
            "spec": {"displayName": display_name, "slug": self._slugify(display_name),
                     "color": "#9e9d24", "cover": ""},
        }
        r = self.s.post(self._url("/apis/content.halo.run/v1alpha1/tags"), json=body,
                        timeout=self.cfg.timeout)
        data = self._check(r, f"创建标签 {display_name}")
        return (data.get("metadata") or {}).get("name", name)

    def ensure_category(self, display_name: str, slug: str | None = None) -> str:
        for c in self.list_categories():
            spec = c.get("spec") or {}
            if spec.get("displayName") == display_name or spec.get("slug") == slug:
                return c["metadata"]["name"]
        name = str(uuid.uuid4())
        body = {
            "apiVersion": "content.halo.run/v1alpha1",
            "kind": "Category",
            "metadata": {"name": name, "generateName": "category-"},
            "spec": {"displayName": display_name, "slug": slug or self._slugify(display_name),
                     "description": "", "cover": "", "template": "", "priority": 0,
                     "children": [], "parent": ""},
        }
        r = self.s.post(self._url("/apis/content.halo.run/v1alpha1/categories"), json=body,
                        timeout=self.cfg.timeout)
        data = self._check(r, f"创建分类 {display_name}")
        return (data.get("metadata") or {}).get("name", name)

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _slugify(text: str, maxlen: int = 80) -> str:
        s = re.sub(r"[\s]+", "-", text.strip().lower())
        s = re.sub(r"[^\w\u4e00-\u9fff\-]", "", s).strip("-")
        s = re.sub(r"-{2,}", "-", s)
        if not s:
            s = hashlib.md5(text.encode()).hexdigest()[:8]
        return s[:maxlen]
