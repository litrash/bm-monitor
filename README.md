# 考生之家报名监控 (bm.e21cn.com)

监控四川"考生之家"在线报名平台（[bm.e21cn.com](https://bm.e21cn.com)）首页，检测报名项目变化并通过微信推送到手机。

## 检测内容

- **新增报名**：首页出现新项目 → 第一时间通知你
- **报名开始**：从未开始变为报名中
- **即将结束**：报名中且距截止 ≤ 阈值（默认 2 小时）
- **报名结束 / 下线**：从进行中列表消失

## 部署到 GitHub（推荐）

### 1. 上传到 GitHub

把这个目录推送到你的 GitHub 仓库（公开或私有都可以）。

### 2. 获取 Server酱 SendKey

去 [sct.ftqq.com](https://sct.ftqq.com) 用微信登录，拿到 SendKey。

### 3. 配置 GitHub Secrets

在仓库 Settings → Secrets and variables → Actions → New repository secret：

| Secret 名称 | 值 | 必填 |
|---|---|---|
| `BM_SC_SENDKEY` | 你的 Server酱 SendKey | ✅ |
| `BM_KEYWORDS` | 关键词过滤, 逗号分隔, 如 `甘孜,社区` | 可选 |
| `BM_AREAS` | 地区过滤, 如 `甘孜藏族自治州` | 可选 |
| `BM_ENDING_HOURS` | 即将结束提醒阈值(小时), 默认 2 | 可选 |

### 4. 手动触发一次

Actions → 考生之家报名监控 → Run workflow → 等它跑完。

首次运行只建立基线，不发通知。之后每次有变化时，微信就会收到 Server酱 推送。

### 运行频率

每天北京时间 8:00-22:00 每 2 小时检查一次。可在 [.github/workflows/monitor.yml](.github/workflows/monitor.yml) 的 `cron` 行调整。

---

## 本地运行

```bash
pip install -r requirements.txt
python bm_monitor.py --once          # 测试抓取
python bm_monitor.py --test-notify   # 测试通知
python bm_monitor.py                 # 循环监控
```

本地运行时，通知方式由 `config.json` 控制（桌面弹窗 / Server酱 / Telegram / 邮件）。

## 可选：Telegram / 邮件通知

| Secret | 说明 |
|---|---|
| `BM_TG_BOT_TOKEN` | Telegram Bot Token |
| `BM_TG_CHAT_ID` | Telegram Chat ID |
| `BM_EMAIL_HOST` ~ `BM_EMAIL_TO` | SMTP 邮件配置 |

## 文件

| 文件 | 说明 |
|---|---|
| `bm_monitor.py` | 主程序 |
| `config.json` | 本地配置（GitHub Actions 中通过 Secrets 配置） |
| `bm_monitor_state.json` | 上次快照（自动维护，GitHub Actions 自动 commit） |
| `.github/workflows/monitor.yml` | GitHub Actions 工作流 |