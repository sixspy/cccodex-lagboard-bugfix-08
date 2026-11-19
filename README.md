# 源到达SLA

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 源到达SLA |
| 目录 | `06-lagboard` |
| 领域建议 | 数据采集、爬虫与 RPA |
| 接口 | http://127.0.0.1:10833 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

源时钟 vs 摄入时钟、分级处罚、降级可读不可写、补齐解冻

## 避开

别做成纯监控大盘；须有写路径与状态机

## 互斥切口候选

- 超SLA冻结 vs 降级只读
- 补齐整源解冻 vs 按业务日窗口解冻
- 只认源时钟 vs 双时钟取严

## 滞后切断口径（已封档）

- **只走甲路径**：策略改口（`sla_minutes`）与未结到达重评分在同一 `BEGIN IMMEDIATE` 锁事务内完成，遇错整体回退，正式档位保持旧值。乙路径（先写意图草稿再翻档）不实现，请求乙路径一律 409。
- **甲乙不得混用**：`/api/sources/{源}` 的 `path` 只能是甲；`display_only=true` 或 `rescore=false` 属偏离口径，拦下并记 `滞后偏了`。
- **未结 / 已结**：未结到达（`settled=0`）随改口重评分；补齐（`/api/catchup/{源}`）把未结置为已结，已结到达在历史附页按封档时的旧档位留痕，不再翻动。
- **滞后切断卡**：每次改口生成一张 `cutover_cards`，当场封档（`status=sealed`）；同日再次改口只追加新卡，旧卡不得改口（PUT/PATCH/DELETE → 405）。
- **双席抢改**：配置席与补采席改同一源须带 `expected_config_version` 占滞后锁；后交返回 409，并记 `时限滞后争用`。缺令牌同样视为后交。
- **采集配置可验收**：`collect_window_minutes` 变更只抬 `config_version`，不动策略档位与计分。
- 五处展示（源名录 / 到达卡片 / 策略列表 / 水位牌 / 历史附页）在改口后同跟新档；只改显示判滞后未过。

失败测见 `tests/test_lag_cutover.py`，覆盖「改口后未结到达仍按旧档计分」复现、翻档失败回退、双席争用留痕、偏离口径拦截、封档不改口、采集配置不动档位。

## 启动（一键）

`powershell
cd projects/06-lagboard
docker compose up -d --build
`

浏览器：http://127.0.0.1:10833/  
健康检查：GET http://127.0.0.1:10833/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10603
python -m pytest -q
`
