# 论坛转 Halo（Forum2Halo）

把论坛帖子一键搬到自己 NAS 上的 [Halo](https://halo.run) 博客：**自动下载正文图片 → 上传到 Halo 附件库 → 重写图片地址 → 排版成文章**。
不用再手动拖图、复制文字和链接。

## 功能

- **批量导入**：一行一个帖子链接，串行处理，实时日志
- **图片自动转存**：逐张下载并上传到 Halo 附件库，正文链接自动替换；单图失败不影响整体，保留原链接并记录
- **智能排版**：剔除分割线/图标等装饰小图，保留文字颜色样式，末尾附来源信息块（原标题/作者/时间/版块/原帖链接）
- **自动打标签**：从标题提取番号（如 `EBWH-356`）作为标签
- **重复导入检测**：按来源链接查重，同一帖子不会重复导入
- **默认存草稿**：先审后发，也可勾选直接发布
- **静默检查更新**：启动时后台查询 GitHub Release，有新版才提示

## 下载安装

到 [Releases](https://github.com/sk20032004/Forum2Halo/releases) 下载 `Forum2Halo-x.x.x-setup.exe` 安装即可。
单文件版 `Forum2Halo.exe` 也可直接运行（免安装）。

## 使用前准备

### 1. 创建 Halo 个人令牌

1. 打开 `http://你的Halo地址/console/` 登录
2. 右上角头像 → **个人中心** → **个人令牌** → **新建**
3. **权限务必勾选完整权限**（含「附件管理」「文章管理」），否则上传图片会返回 403
4. 复制生成的 `pat_` 开头字符串（只显示一次）

### 2. 配置代理

如果所在网络访问论坛需要代理，在「抓取设置 → 代理」填写，例如 `http://127.0.0.1:7897`。
访问 Halo 走局域网，会自动绕过代理。

## 使用

1. 双击运行 → 右侧填 Halo 地址和令牌 → 点「测试连接」
2. 左侧粘贴帖子链接（一行一个）
3. 先点「只测试解析（不上传）」确认解析正常
4. 点「开始导入」，完成后到 Halo 后台审核草稿并发布

## 支持的论坛

目前支持 **Discuz! X** 系论坛（如 javbus 论坛）。Discuz 结构统一，其他 Discuz 站点改下域名通常可直接用。

解析逻辑在 `fetcher.py`，新增站点只需继承 `BaseParser` 并实现 `match()` 和 `parse()`，
其余流程（下载、上传、排版、查重）完全复用。

## 命令行用法

```bash
# 单个，存草稿
python cli.py "https://www.javbus.com/forum/forum.php?mod=viewthread&tid=177470"

# 从文件批量导入并直接发布
python cli.py -f urls.txt --publish

# 只解析不上传
python cli.py --dry-run "https://..."
```

## 开发

```bash
pip install -r requirements.txt
python desktop.py     # 桌面版
python app.py         # Web 版（http://127.0.0.1:8765）
```

打包：

```bash
pyinstaller --onefile --windowed --name Forum2Halo --icon app.ico desktop.py
ISCC.exe installer.iss    # 生成安装包到 release/
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `desktop.py` | 桌面版主程序（tkinter） |
| `app.py` | Web 版控制台（Flask） |
| `cli.py` | 命令行工具 |
| `fetcher.py` | 论坛页面抓取与解析 |
| `halo_client.py` | Halo API 封装 |
| `importer.py` | 导入流程编排 |
| `updater.py` | 版本检查 |
| `installer.iss` | 安装包脚本 |

## 常见问题

**上传图片报 403？** 令牌权限不足。重建令牌时勾选完整权限。

**报 `Group/xxx was not found`？** 早期版本的附件分组问题，已修复（自动创建分组）。

**帖子抓不到？** 部分帖子需登录可见，在「论坛 Cookie」填入浏览器 Cookie。

**配置和令牌存在哪？** `config.json`（程序目录下）。点「清除用户数据」可一键删除配置和已下载的图片存档。

## License

MIT
