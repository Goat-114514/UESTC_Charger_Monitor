# 充电历史库

这一套工具负责把 `all.json` 里的站点按固定周期持续采样，形成本地历史库（保留天数可配，默认 14 天），并提供查询界面。

## 目录

- `charge_history_backend.py`：采样后端和 HTTP 接口
- `charge_history_ui.py`：PyQt5 查询前端
- `charge_history_seed.py`：生成演示数据库
- `all.json`：站点清单
- `charge_history_demo.db`：演示数据

## 本地运行

先启动后端：

```powershell
python charge_history/charge_history_backend.py
```

再启动前端：

```powershell
python charge_history/charge_history_ui.py
```

## 演示数据

如果想先看界面效果，可以直接生成演示数据库：

```powershell
python charge_history/charge_history_seed.py
```

演示库默认写到当前目录下的 `charge_history_demo.db`，不会污染真实连续采样库。

## 常用调试命令

只采样前 5 个站点：

```powershell
python charge_history/charge_history_backend.py --station-limit 5
```

缩短采样间隔到 10 秒：

```powershell
python charge_history/charge_history_backend.py --sample-interval 10
```

只跑接口，不自动采样：

```powershell
python charge_history/charge_history_backend.py --db charge_history/charge_history_demo.db --no-collector
```

## 采样能力与站点规模

上游接口是「1 次站点列表 + 每个占用插座 1 次详情」的 N+1 模式，且服务端有总吞吐上限。
实测（2026-10，121 站 / 952 插座 / 约 750 个占用中）：

| 站点数 | 单轮耗时 | 60s 采样 | 20~30 分钟采样 |
| --- | --- | --- | --- |
| 6~7 | 约 15~17s | 可以 | 轻松 |
| 24~28 | 约 60s | 勉强（刚好一轮一分钟） | 轻松 |
| 121 | 约 227~246s（4 分钟） | 不可以 | 轻松，余量充足 |

`--station-workers` 4 和 8 差别很小（246s vs 227s），再往上收益趋零，
反而会让单请求延迟从 0.7s 劣化到 2.9s。

采样循环按**整点刻度对齐**：间隔 1200 秒时，采集时刻就是 18:20、18:40、19:00 这种整点，
`sample_time` 也直接记成刻度时间（如 `18:20:00`），所以每轮之间的间隔严格相等。
单轮耗时超过间隔时会跳过一个刻度，并在日志里打印警告。

启动时会先立刻采一轮（不受刻度约束），之后才进入整点节奏。

## 连续采集一到两周

现在的默认值就是按长期采集配好的：**20 分钟一轮、整点对齐、数据永不清理**。

```powershell
python charge_history/charge_history_backend.py
```

需要改周期时加参数（`--sample-interval 1800` 就是每半小时的 :00 和 :30）：

```powershell
python charge_history/charge_history_backend.py --sample-interval 1800
```

### 无人值守（任务计划程序）

`start_collector.bat` 已经配好，注册成计划任务后开机登录即自动运行：

```powershell
Start-ScheduledTask -TaskName "ChargerHistory"
```

任务 `ChargerHistory` 的关键设置：

- 触发器：用户登录时
- **执行时限：无限制**（默认是 3 天，不改的话两周的任务会被系统掐断）
- 失败后每 1 分钟重启，最多 999 次
- 使用电池时也启动、不因切换到电池而停止
- 不重复启动（已在跑就忽略）
- 输出写到 `logs/collector.log`

查看状态：

```powershell
Get-ScheduledTaskInfo -TaskName "ChargerHistory"
```

### 数据存放

- 默认目录：`charge_history/charge_data/`
- 文件名按启动时间编号：`charge_20261020_175905.db`
- **长期采集要固定 `--db`**（`start_collector.bat` 里已经写死），这样重启后接着写同一个库；
  不指定 `--db` 时每次启动都会新建一个文件，重启会把数据分叉成几个库，而前端一次只能看一个
- `--retention-days` 默认 `0`，表示永不清理；填大于 0 才按天数删旧数据
- 想换位置用 `--data-dir D:\charge_data`

需要留意的几点：

- **磁盘占用**：一轮约 0.3~0.4 MB（952 条采样 + 约 750 条会话采样）。
  20 分钟一轮约 72 轮/天，两周约 1000 轮，预计 **300~400 MB**。
- **时间戳偏差**：整轮共用一个 `sample_time`，在整轮结束时打点，
  所以第一个被采的站点数据最多比时间戳早一个单轮耗时（约 4 分钟）。
  20 分钟间隔下占 20%，可接受；同一插座跨轮的时间间隔是稳定的。
- **单轮原子性**：整轮采完才批量写库，进程在单轮中途被杀则该轮数据全部丢失。
- **进程守护**：程序本身不会自愈，断电/重启/崩溃后不会自动恢复。
  长时间无人值守建议用「任务计划程序」或 `nssm` 把它注册成开机自启的服务。
- **断电无法补采**：上游只返回当前状态，关机期间的采样点永久丢失。
  要彻底解决需要同时跑一路云端采集，见下面的「云端备份采集（GitHub Actions）」。
- **网络中断**：断网期间该轮记为失败（`station_failed`），恢复后自动继续，不会补采。

## 云端备份采集（GitHub Actions）

本地采集器的死穴是「电脑不能关机」：断电、蓝屏、系统更新重启，那一轮就永久丢了，
而且上游接口只给当前状态，**事后无法补采**。所以想彻底补上缺漏，只能再找一个
7×24 在线的机器同时采一份。

GitHub Actions 的定时任务可以充当这个备份采集器，思路是：

```
GitHub runner（每 20 分钟） --采集--> gzip JSONL 分片 --提交--> 仓库的 data 分支
                                                                    |
本地电脑（开机时） --git fetch--> 分片 --gha_merge.py 重放--> SQLite --> 前端
```

时间刻意错开：本地跑 `:00 / :20 / :40`，云端跑 `:10 / :30 / :50`。
两边都正常时合起来是 10 分钟一个采样点；本地挂了云端照常采，反之亦然。

### 相关文件

| 文件 | 作用 |
| --- | --- |
| `.github/workflows/collect.yml` | 定时触发、采集、把分片提交到 `data` 分支 |
| `charge_history/gha_collect.py` | 一次性采一轮，输出 gzip JSONL 分片 |
| `charge_history/gha_merge.py` | 把分片按时间顺序重放进 SQLite |

`gha_merge.py` 直接复用后端 `HistoryDatabase.record_station_snapshot`，
所以表结构、会话切分（`charge_sessions` / `session_samples`）和本地采集完全一致，
前端不用改一行代码。

### 为什么不在 Actions 里直接写 SQLite

一次运行一个分片文件，git 只做追加、不重写历史。如果每轮都「下载整库 → 写入 → 提交回去」，
一天 72 次提交、每次都是 30MB 级的完整库文件，仓库历史很快会被撑爆。
分片模式单文件几十 KB，天然无冲突，也方便按日期裁剪。

### 部署步骤

1. 在 GitHub 建一个仓库，把本项目推上去（`.gitignore` 已排除 `charge_data/`、`logs/`）：

   ```bash
   git init
   git add .
   git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<你>/<仓库>.git
   git push -u origin main
   ```

2. 什么都不用配。Actions 默认的 `GITHUB_TOKEN` 有写权限（workflow 里已声明
   `permissions: contents: write`），第一次运行会自动创建 `data` 分支。

3. 到仓库的 **Actions** 页，选中 `charger-collect`，点 **Run workflow**，
   把 `station_limit` 填 `3` 先试跑一次，确认能采到数据、能提交分片。

4. 试跑产生的分片只有 3 个站，建议删掉：到 `data` 分支里把对应日期目录下的那个
   `*.jsonl.gz` 删掉再提交，避免和后面的全量数据混在一起（`gha_merge.py` 也能容忍它，
   只是那一个采样点的站点数会明显偏少）。

### 额度与限制（重要）

| 项目 | 说明 |
| --- | --- |
| **公开仓库** | Actions 分钟数不限，20 分钟一轮随便跑 |
| **私有仓库** | 免费版每月 2000 分钟。一轮全量采集约 4~8 分钟，72 轮/天 ≈ 300~570 分钟/天，**会严重超额** |
| 建议 | 私有仓库要么把 cron 改成 `0 */3 * * *`（3 小时一轮，约 1440 分钟/月），要么升级付费 |
| 触发精度 | GitHub 的 cron 不是精确闹钟，高峰期会延迟几分钟到十几分钟，无法保证整点 |
| 60 天规则 | 公开仓库若连续 60 天没有任何提交，定时任务会被自动停用。这里每轮都会提交分片，天然不会触发 |
| 网络 | runner 在境外，请求上游接口比国内慢一些，全量一轮可能到 5~8 分钟；偶发被限流会记为失败站 |

### 本地合并

先把 `data` 分支的数据同步到本地（在仓库根目录执行）：

```bash
git fetch origin data
mkdir -p gha_data
git archive --format=tar origin/data | tar -x -C gha_data
```

再合并。两种用法：

```powershell
# 合并成一个独立的库（安全，默认写 charge_history/charge_data/gha_merged.db）
python charge_history/gha_merge.py

# 直接合并进正在采集的本地库，前端一次就能看到两路数据
python charge_history/gha_merge.py --db charge_history/charge_data/charge_20261020_175905.db
```

- 已合并的分片记在 `ingest_shards` 表里，**重复执行不会产生重复数据**
- 采样行本身还有 `UNIQUE(sample_time, outlet_no)` 兜底，两路数据时间撞了也不会重复
- 合并进正在跑的库时用的是 WAL + 30s busy_timeout，短暂锁等待会自动重试
- 想从头重放一遍：加 `--reset`（不会删除已有采样，只是重新过一遍分片）

### 分片格式

`data` 分支下的路径是 `YYYY-MM-DD/HHMMSS[_runid].jsonl.gz`，每行一个 JSON：

```json
{"v":1,"kind":"meta","sample_time":"2026-10-20T15:30:00+00:00","station_ok":121,...}
{"v":1,"kind":"station","station_id":120204,"station_name":"...","outlets":[...]}
{"v":1,"kind":"failed","station_id":120204,"station_name":"...","error":"接口返回空数据"}
```

目录名 + 文件名就是字典序 = 时间序，合并时按这个顺序重放。

### 体积估算

单轮 gzip 后约 30~60 KB，72 轮/天 ≈ 3~4 MB/天，一个月约 100 MB。
GitHub 单仓库建议不超过 1 GB，长期跑建议定期裁剪：

```bash
# 只保留最近 90 天的分片
git clone --branch data --single-branch <仓库> data-repo
cd data-repo && ls -d 2026-0* | head -n -3 | xargs rm -rf
git add -A && git commit -m "prune" && git push
```

## 前端连哪个后端

前端默认连本机 `http://127.0.0.1:8765`。要改成远程后端，三种方式任选：

- 环境变量：`set CHARGE_SERVER_URL=https://wgooold.cn:8765`
- 在 `charge_history_ui.py` 同目录放 `charge_history_ui_config.json`：

```json
{ "server_url": "https://wgooold.cn:8765" }
```

- 直接改 `charge_history_ui.py` 里的 `DEFAULT_SERVER_URL`

部署到云服务器时，后端需要满足下面两种方案之一：

- 方案 1：后端自己直接提供 HTTPS
- 方案 2：由 Nginx / Caddy 等反向代理在 `https://wgooold.cn:8765` 接入，再转发到本地 HTTP 后端

如果后端要自己直接对外监听 HTTPS，可以这样启动：

```powershell
python charge_history/charge_history_backend.py --host 0.0.0.0 --port 8765 --ssl-cert /path/to/fullchain.pem --ssl-key /path/to/privkey.pem
```

如果只是本地 HTTP 测试，对外监听可以这样启动：

```powershell
python charge_history/charge_history_backend.py --host 0.0.0.0
```

## 主要接口

- `GET /api/health`
- `GET /api/stations`
- `GET /api/outlets?station_id=120204`
- `GET /api/sessions?station_id=120204&outlet_no=O22071401189593&days=3`
- `GET /api/session?session_id=1`
- `GET /api/admin/collect`
