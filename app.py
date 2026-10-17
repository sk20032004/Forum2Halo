"""本地 Web 控制台：粘贴链接 → 一键导入 Halo。

启动：
    .venv/Scripts/python.exe app.py
然后浏览器打开 http://127.0.0.1:8765
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import traceback
import uuid

from flask import Flask, Response, jsonify, request, send_file

from halo_client import HaloClient, HaloConfig
from importer import ImportResult, Importer, Settings

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
WORK_ROOT = os.path.join(BASE, "work")

app = Flask(__name__, static_folder=None)

# 任务表：job_id -> {status, lines, result}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def load_settings() -> Settings:
    return Settings.from_file(CONFIG_PATH)


def save_settings(st: Settings) -> None:
    st.save(CONFIG_PATH)


def mask(token: str) -> str:
    if not token:
        return ""
    return token[:6] + "***" + token[-4:] if len(token) > 12 else "***"


# --------------------------------------------------------------------- 页面
@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.get("/api/settings")
def get_settings():
    st = load_settings()
    d = {k: getattr(st, k) for k in st.__dataclass_fields__}
    d["halo_token"] = mask(st.halo_token)
    d["has_token"] = bool(st.halo_token)
    return jsonify(d)


@app.post("/api/settings")
def set_settings():
    body = request.get_json(force=True)
    st = load_settings()
    for k, v in body.items():
        if k in st.__dataclass_fields__ and k != "halo_token":
            setattr(st, k, v)
    if body.get("halo_token"):  # 空字符串表示不修改
        st.halo_token = body["halo_token"].strip()
    save_settings(st)
    return jsonify({"ok": True})


@app.post("/api/test-halo")
def test_halo():
    st = load_settings()
    # 支持前端直接传令牌/地址来测试（不必先保存）
    body = request.get_json(silent=True) or {}
    if body.get("halo_token"):
        st.halo_token = body["halo_token"].strip()
    if body.get("halo_url"):
        st.halo_url = body["halo_url"].strip()
    if not st.halo_token:
        return jsonify({"ok": False, "message": "还没有填令牌，请先在上方粘贴 pat_ 开头的个人令牌"}), 200
    try:
        c = HaloClient(HaloConfig(base_url=st.halo_url, token=st.halo_token))
        info = c.ping()
        me = {}
        try:
            me = c.me()
        except Exception:
            pass
        return jsonify({
            "ok": True,
            "message": f"连接成功，现有文章 {info['total']} 篇",
            "user": (me.get("spec") or {}).get("displayName", ""),
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)}), 200


@app.post("/api/test-forum")
def test_forum():
    """只抓取 + 解析，不上传，用于验证代理和 Cookie。"""
    st = load_settings()
    body = request.get_json(force=True)
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "message": "请填写帖子地址"})
    try:
        imp = Importer(st)
        data, _ = imp.fetch_thread(url)
        return jsonify({
            "ok": True,
            "title": data.title,
            "author": data.author,
            "posted_at": data.posted_at,
            "forum": data.forum_name,
            "images": len(data.images),
            "text_len": len(re.sub(r"<[^>]+>", "", data.content_html)),
            "message": "解析成功",
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)})


@app.get("/api/categories")
def categories():
    st = load_settings()
    try:
        c = HaloClient(HaloConfig(base_url=st.halo_url, token=st.halo_token))
        return jsonify({"ok": True, "items": [
            (x.get("spec") or {}).get("displayName") for x in c.list_categories()
        ]})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e), "items": []})


@app.post("/api/import")
def start_import():
    """提交一批链接，后台线程逐个导入，前端通过 /api/stream/<job> 看进度。"""
    body = request.get_json(force=True)
    urls = [u.strip() for u in (body.get("urls") or []) if u.strip()]
    if not urls:
        return jsonify({"ok": False, "message": "没有有效链接"}), 400

    job_id = uuid.uuid4().hex[:12]
    allow_dup = bool(body.get("allow_duplicate"))
    st = load_settings()
    # 本次任务允许前端临时覆盖的几个开关
    if body.get("publish") is not None:
        st.publish = bool(body["publish"])
    if body.get("category") is not None:
        st.category = body["category"]

    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "lines": [], "results": [],
                        "total": len(urls), "index": 0, "q": queue.Queue()}

    def run():
        job = JOBS[job_id]

        def log(msg: str):
            job["q"].put({"type": "log", "msg": msg})

        for i, url in enumerate(urls, 1):
            log(f"\n━━━ [{i}/{len(urls)}] {url}")
            work_dir = os.path.join(WORK_ROOT, _safe_name(url))
            try:
                imp = Importer(st, log=log)
                res = imp.run(url, work_dir, skip_duplicate=not allow_dup)
                job["results"].append({
                    "url": url, "ok": res.ok, "title": res.title,
                    "message": res.message, "post_url": res.post_url,
                    "images": f"{res.images_ok}/{res.images_total}",
                    "duplicate": res.skipped_duplicate,
                })
                log(f"结果：{res.message}（图片 {res.images_ok}/{res.images_total}）")
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                job["results"].append({"url": url, "ok": False, "message": str(e)})
                log(f"失败：{e}")
            job["index"] = i
            job["q"].put({"type": "done_item", "index": i, "total": len(urls),
                          "result": job["results"][-1]})
        job["status"] = "finished"
        job["q"].put({"type": "end"})

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "job": job_id, "count": len(urls)})


@app.get("/api/stream/<job_id>")
def stream(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "message": "任务不存在"}), 404

    def gen():
        q: queue.Queue = job["q"]
        while True:
            try:
                item = q.get(timeout=30)
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
                continue
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            if item.get("type") == "end":
                break

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs")
def jobs():
    with JOBS_LOCK:
        return jsonify([
            {"id": k, "status": v["status"], "index": v["index"], "total": v["total"],
             "results": v["results"]}
            for k, v in JOBS.items()
        ])


def _safe_name(url: str) -> str:
    tid = re.search(r"tid=(\d+)", url)
    tag = tid.group(1) if tid else uuid.uuid4().hex[:8]
    host = re.sub(r"[^\w.]", "", url.split("//")[-1].split("/")[0])
    return f"{host}_{tag}_{int(time.time())}"


# --------------------------------------------------------------------- 前端
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>论坛转 Halo · 导入台</title>
<style>
  :root{
    --bg:#f5f6f8; --card:#fff; --line:#e3e6ea; --text:#1c2128; --sub:#68717d;
    --brand:#2f6fed; --ok:#1a9c5b; --warn:#d98300; --err:#d33;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:var(--sub);font-size:13px;margin-bottom:20px}
  .grid{display:grid;grid-template-columns:1fr 320px;gap:16px}
  @media(max-width:900px){.grid{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
  .card h2{font-size:14px;margin:0 0 12px;font-weight:600}
  textarea,input,select{width:100%;border:1px solid var(--line);border-radius:7px;
    padding:8px 10px;font:inherit;background:#fff;color:var(--text)}
  textarea{min-height:130px;resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:13px}
  label{display:block;font-size:12px;color:var(--sub);margin:10px 0 4px}
  .row{display:flex;gap:8px;align-items:center}
  button{border:0;border-radius:7px;padding:9px 16px;font:inherit;cursor:pointer;
    background:#eef1f5;color:var(--text)}
  button.primary{background:var(--brand);color:#fff}
  button.ghost{background:#fff;border:1px solid var(--line)}
  button:disabled{opacity:.5;cursor:not-allowed}
  .hint{font-size:12px;color:var(--sub);margin-top:6px}
  #log{background:#12161c;color:#d6e0ea;border-radius:8px;padding:12px;
       height:340px;overflow:auto;font:12.5px/1.6 ui-monospace,Consolas,monospace;
       white-space:pre-wrap;word-break:break-all;margin-top:14px}
  .badge{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;margin-left:6px}
  .b-ok{background:#e3f6ec;color:var(--ok)}
  .b-err{background:#fdeaea;color:var(--err)}
  .b-warn{background:#fff4e0;color:var(--warn)}
  .status{font-size:12px;margin-top:8px;min-height:18px}
  table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:10px}
  th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--sub);font-weight:500}
  a{color:var(--brand);text-decoration:none}
  .switch{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--text);margin-top:10px}
  .switch input{width:auto}
  details summary{cursor:pointer;color:var(--sub);font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <h1>论坛帖子 → Halo 博客 导入台</h1>
  <div class="sub">批量粘贴帖子链接，自动下载图片转存到 Halo 附件库，并把正文排版成文章。默认存草稿，确认无误后再发布。</div>

  <div class="grid">
    <div>
      <div class="card">
        <h2>待导入链接（一行一个）</h2>
        <textarea id="urls" placeholder="https://www.javbus.com/forum/forum.php?mod=viewthread&tid=177470
https://www.javbus.com/forum/forum.php?mod=viewthread&tid=177471"></textarea>
        <div class="switch">
          <input type="checkbox" id="publish" />
          <label for="publish" style="margin:0">直接发布（不勾选则存为草稿）</label>
          <input type="checkbox" id="allowDup" style="margin-left:14px" />
          <label for="allowDup" style="margin:0">允许重复导入</label>
        </div>
        <div class="row" style="margin-top:14px">
          <button class="primary" id="start">开始导入</button>
          <button class="ghost" id="testForum">只测试解析（不上传）</button>
          <span class="status" id="status"></span>
        </div>
        <div id="log">就绪。先在右侧填好 Halo 地址和令牌，然后点「开始导入」。</div>
        <table id="results" style="display:none">
          <thead><tr><th>帖子</th><th>结果</th><th>图片</th><th></th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>

    <div>
      <div class="card">
        <h2>Halo 设置</h2>
        <label>博客地址</label>
        <input id="halo_url" placeholder="http://192.168.5.220:8556" />
        <label>个人令牌（pat_ 开头）</label>
        <input id="halo_token" type="password" placeholder="保持不变则留空" />
        <div class="hint">后台 → 右上角头像 → 个人中心 → 个人令牌 → 新建</div>
        <div class="row" style="margin-top:12px">
          <button class="ghost" id="testHalo">测试连接</button>
          <span class="status" id="haloStatus"></span>
        </div>
        <label style="margin-top:14px">分类（留空则不分类）</label>
        <input id="category" list="catList" placeholder="论坛收藏" />
        <datalist id="catList"></datalist>
      </div>

      <div class="card" style="margin-top:16px">
        <h2>抓取设置</h2>
        <label>代理（访问论坛需要）</label>
        <input id="proxy" placeholder="http://127.0.0.1:7897" />
        <label>论坛 Cookie（可选，需要登录的帖子才填）</label>
        <textarea id="cookie" style="min-height:64px;font-size:12px" placeholder="留空表示以游客身份抓取"></textarea>
        <details style="margin-top:12px">
          <summary>高级</summary>
          <label>图片 Referer（防盗链时用）</label>
          <input id="referer" placeholder="默认取论坛首页" />
          <label>并发数</label>
          <input id="threads" type="number" min="1" max="8" />
          <label>单图大小上限 (MB)</label>
          <input id="max_image_mb" type="number" min="1" max="200" />
          <div class="switch">
            <input type="checkbox" id="add_source_block" />
            <label for="add_source_block" style="margin:0">文末附来源信息块</label>
          </div>
          <div class="switch">
            <input type="checkbox" id="tag_from_title" />
            <label for="tag_from_title" style="margin:0">从标题提取番号作标签</label>
          </div>
        </details>
        <div class="row" style="margin-top:12px">
          <button class="primary" id="save">保存全部设置</button>
          <span class="status" id="saveStatus"></span>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const logEl = $("log");
let currentJob = null;

function appendLog(text, cls) {
  const span = document.createElement("span");
  span.textContent = text + "\n";
  if (cls) span.style.color = cls;
  logEl.appendChild(span);
  logEl.scrollTop = logEl.scrollHeight;
}

async function loadSettings() {
  const r = await fetch("/api/settings").then(r => r.json());
  $("halo_url").value = r.halo_url || "";
  $("category").value = r.category || "";
  $("proxy").value = r.proxy || "";
  $("cookie").value = r.cookie || "";
  $("referer").value = r.referer || "";
  $("threads").value = r.threads ?? 3;
  $("max_image_mb").value = r.max_image_mb ?? 50;
  $("add_source_block").checked = !!r.add_source_block;
  $("tag_from_title").checked = !!r.tag_from_title;
  $("publish").checked = !!r.publish;
  $("halo_token").placeholder = r.has_token ? "已保存（留空不修改）" : "pat_xxxxxxxx";
  if (r.has_token) testHalo(true);
}

async function saveSettings() {
  const body = {
    halo_url: $("halo_url").value.trim(),
    category: $("category").value.trim(),
    proxy: $("proxy").value.trim(),
    cookie: $("cookie").value.trim(),
    referer: $("referer").value.trim(),
    threads: parseInt($("threads").value) || 3,
    max_image_mb: parseInt($("max_image_mb").value) || 50,
    add_source_block: $("add_source_block").checked,
    tag_from_title: $("tag_from_title").checked,
  };
  const t = $("halo_token").value.trim();
  if (t) body.halo_token = t;
  const r = await fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  $("saveStatus").textContent = r.ok ? "已保存" : "保存失败";
  setTimeout(() => $("saveStatus").textContent = "", 2500);
  loadCategories();
}

async function testHalo(silent) {
  $("haloStatus").textContent = "测试中…";
  const body = {halo_url: $("halo_url").value.trim()};
  const t = $("halo_token").value.trim();
  if (t) body.halo_token = t;   // 直接用表单里的令牌测试，不用等保存
  const r = await fetch("/api/test-halo", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}).then(r => r.json());
  $("haloStatus").textContent = (r.ok ? "✅ " : "❌ ") + r.message + (r.user ? "（" + r.user + "）" : "");
  $("haloStatus").style.color = r.ok ? "var(--ok)" : "var(--err)";
  if (!silent && r.ok) loadCategories();
}

async function loadCategories() {
  const r = await fetch("/api/categories").then(r => r.json());
  $("catList").innerHTML = (r.items || []).map(x => `<option value="${x}">`).join("");
}

$("testHalo").onclick = () => testHalo(false);
$("save").onclick = saveSettings;

$("testForum").onclick = async () => {
  const urls = $("urls").value.split("\n").map(s => s.trim()).filter(Boolean);
  if (!urls.length) return ($("status").textContent = "先填一条链接");
  $("status").textContent = "解析中…";
  await saveSettings();
  const r = await fetch("/api/test-forum", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({url: urls[0]})}).then(r => r.json());
  $("status").textContent = "";
  if (r.ok) {
    appendLog(`✓ 解析成功\n  标题：${r.title}\n  作者：${r.author}  时间：${r.posted_at}  版块：${r.forum}\n  图片：${r.images} 张，正文 ${r.text_len} 字`, "#7fd");
  } else {
    appendLog(`✗ ${r.message}`, "#f99");
  }
};

$("start").onclick = async () => {
  const urls = $("urls").value.split("\n").map(s => s.trim()).filter(Boolean);
  if (!urls.length) return alert("请先粘贴帖子链接");
  await saveSettings();
  const btn = $("start");
  btn.disabled = true;
  logEl.innerHTML = "";
  $("results").style.display = "none";
  $("results").querySelector("tbody").innerHTML = "";

  const r = await fetch("/api/import", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({urls, publish: $("publish").checked, allow_duplicate: $("allowDup").checked,
                          category: $("category").value.trim()})}).then(r => r.json());
  if (!r.ok) { btn.disabled = false; return appendLog("提交失败：" + r.message, "#f99"); }

  currentJob = r.job;
  appendLog(`共 ${r.count} 个链接开始处理…`);
  const es = new EventSource(`/api/stream/${r.job}`);
  es.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "log") appendLog(m.msg);
    if (m.type === "done_item") {
      $("status").textContent = `${m.index}/${m.total}`;
      if (m.result) addResultRow(m.result);
    }
    if (m.type === "end") { es.close(); btn.disabled = false; $("status").textContent = "全部完成"; appendLog("\n— 全部完成 —", "#7fd"); }
  };
  es.onerror = () => { es.close(); btn.disabled = false; };
};

function addResultRow(r) {
  const t = $("results");
  t.style.display = "table";
  const tr = document.createElement("tr");
  const badge = r.ok ? (r.duplicate ? '<span class="badge b-warn">已存在</span>' : '<span class="badge b-ok">成功</span>')
                     : '<span class="badge b-err">失败</span>';
  tr.innerHTML = `<td>${escapeHtml(r.title || r.url)}</td>
    <td>${badge} ${escapeHtml(r.message || "")}</td>
    <td>${r.images || "-"}</td>
    <td>${r.post_url ? `<a href="${r.post_url}" target="_blank">后台打开</a>` : ""}</td>`;
  t.querySelector("tbody").appendChild(tr);
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

loadSettings().then(loadCategories);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    os.makedirs(WORK_ROOT, exist_ok=True)
    port = int(os.environ.get("PORT", "8765"))
    print(f"\n  论坛转 Halo 控制台：http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
