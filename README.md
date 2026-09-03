# JD online — JAVDB 媒体管理中心

一个自托管的 **JAVDB 媒体库 / 下载 / 推送** 管理中心。逆向自 JAVDB 移动端 API + JAVBUS 磁链 + Emby/Jellyfin 入库联动，网页界面（Flask）+ SQLite 存储。**仅供个人 / 家庭内网使用。**

> 曾用名 **JD Center**，现已更名为 **JD online**（v1.0.0）。

> ⚠️ **安全提醒**：默认登录口令 `123 / abc123` 为弱口令，**部署后请立即在「设置」页修改**；若暴露公网请务必加反代 + HTTPS 鉴权。

---

## 功能

- **媒体库**：搜索、抓取、影片详情（封面 / 预览图 / 演员 / 磁链 / 评论），JAVBUS 磁链自动解析（名称 / 大小 / 日期 / HD / 字幕 / 破解）。
- **榜单**：Top250、日 / 周 / 月榜、演员榜、订阅榜单。
- **订阅系统**：影片 / 在线 / 演员 / 清单订阅 + 黑名单；条件弹窗（质量 / 下载模式 / 文件大小 / 上映日期 / 类别），后台定时检查与自动推送，失败自动换磁链重试。
- **115 网盘集成**：磁链一键推送离线下载，任务状态轮询，失败重试。
- **CloudDrive2 集成**：内置纯 Python 版 h2c gRPC 客户端，自动挂载 / 识别库内文件。
- **Emby / Jellyfin 联动**：全量同步媒体库、番号索引、入库状态。
- **手机适配**：设置页 / 榜单 / 订阅弹窗均做了响应式（单列 / 两列自动折行）。

---

## 快速开始（Docker）

镜像名：`desstg/jd-online:latest`（Docker Hub）

```bash
# 拉取
docker pull desstg/jd-online:latest

# 1) 直接运行（数据先落在一个数据卷）
docker run -d --name jd-online \
  -p 9091:9091 \
  -v jd-online-data:/data \
  --restart unless-stopped \
  desstg/jd-online:latest
```

或用 `docker-compose.yml`（更推荐，含说明注释）：

```bash
mkdir -p ./data
docker compose up -d
```

启动后访问 `http://<主机>:9091`。

> **登录口令**：默认 `123 / abc123`。可在「设置」页修改，或在 compose 里用环境变量直接指定（**优先级最高**，覆盖 config.json 与默认值）：
> ```yaml
> environment:
>   - JD_WEB_USERNAME=你的用户名
>   - JD_WEB_PASSWORD=你的密码
> ```

### 持久化与迁移

- 运行数据全部在容器内 `/data` 卷：`config.json`（网络 / 凭据 / 订阅设置）+ `javdb.db`（媒体库 / 记录）。
- 全新启动：`./data` 留空，程序自动生成默认配置与空数据库，之后在网页「设置」页填入 JAVDB、代理、Emby、115 等凭据。
- 迁移现有数据：把原机的 `config.json`、`javdb.db` 复制进 `./data` 挂载目录即可，番号、订阅、推送记录全部保留。

### 权限说明

容器以**非 root** `appuser(uid 1000)` 运行。绑定挂载宿主机目录时需可写：

```bash
chown -R 1000:1000 ./data
```

如果嫌麻烦，可在 compose 里加 `user: "0:0"` 以 root 运行（不推荐）。

---

## 配置项（设置页或 config.json）

| 配置 | 说明 |
|---|---|
| `web_username` / `web_password` | 网页登录口令 |
| `javdb_username` / `javdb_password` / `javdb_token` | JAVDB 移动端 API 凭据 |
| `proxy` | 网络代理（JAVDB / JAVBUS / 图片都走它） |
| `api_base` / `javbus_base` | JAVDB API 节点、JAVBUS 域名（被墙/换域名时改这里） |
| `min_interval` | 抓取限流间隔（秒） |
| `port` / `host` | 监听地址（默认 9091 / 0.0.0.0） |
| 订阅 `sub_*` | 订阅调度：每日检查时间 / 检查间隔 / 通道并发 / 超时 / 重试 / 同步时间表 |

> **不要把含真实凭据的 `config.json` 提交到公开仓库。**

---

## 本地开发运行

```bash
pip install -r requirements.txt
python webapp.py          # 默认 http://0.0.0.0:9091
```

命令行管线（`main.py`）：

```bash
python main.py search "SSIS-001"                      # 搜索
python main.py detail "SSIS-001" --magnets            # 详情+演员+磁链
python main.py hot                                    # 热播榜整榜入库
python main.py server-add --name 客厅Emby --url http://192.168.1.10:8096 --api-key KEY --type emby
python main.py sync                                   # 全量同步媒体库
```

---

## 数据来源

| 数据 | 接口 | 是否需登录 |
|---|---|---|
| 搜索 | JAVDB `/v2/search` | 否 |
| 影片详情 | JAVDB `/v4/movies/{id}` | 否 |
| 评论 | JAVDB `/v1/movies/{id}/reviews` | 否 |
| 热播榜 / Top250 | JAVDB `/v1/rankings/playback`、`/v1/movies/top` | Top250 需登录 |
| 磁链 | JAVBUS `ajax/uncledatoolsbyajax.php` | 否 |
| 下载推送 | 115 网盘离线接口（需 Cookie） | 是 |
| 媒体库 | Emby/Jellyfin API | 需 API Key |

---

## 镜像说明

- **代码**：`/app`（Flask + `javdb/`），**镜像内不含任何数据库 / 凭据**。
- **数据**：`/data`（`config.json`、`javdb.db`），挂载卷持久化。
- **运行**：`python /app/webapp.py`，监听 9091。
- 依赖：`flask`、`h2`（CloudDrive2 必需）、`p115client`（115 推送，可注释掉降级）。

---

## 免责声明

本项目仅供学习交流 / 个人内网使用。**不包含、不代理、不上传任何影片内容**，所有数据均来自外部公开接口（JAVDB / JAVBUS / 115 / Emby / Jellyfin），请自行遵守当地法律法规与所使用服务的条款。

代码按「原样」提供，**无任何担保**；若官方接口变更或密钥轮换，项目可能失效（相关签名密钥在 `javdb/client.py`，官方一旦轮换需自行更新）。
