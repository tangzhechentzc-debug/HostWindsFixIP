# Hostwinds 节点 IP 自动巡检与修复系统 · PRD

| 项 | 值 |
| --- | --- |
| 版本 | 1.3（已上线） |
| 日期 | 2026-08-27 |
| 状态 | 生产运行中（systemd timer 已启用） |
| 运行环境 | 订阅服务器 203.0.113.10 · Ubuntu · Python 3.12.3 |

---

## 1. 背景与问题

自建 Shadowrocket / Clash 订阅的出口节点是一台 Hostwinds VPS。该节点 IP 会被 GFW 封锁，
表现为**国内 ICMP/TCP 不通、国外仍通**。一旦被封，订阅里的节点即失效。

封锁前的人工流程：

1. 察觉翻墙失败
2. 登录 Hostwinds 控制台，点 **Fix ISP Block** 换 IP
3. 等约 20 分钟生效
4. 用检测工具确认新 IP 未被封（可能仍被封，需重复 2–3）
5. 手工把新 IP 填进 `ips.txt`，推送并重新生成订阅

痛点：**全程人工、发现滞后、易漏步**。实际发生过 `ips.txt` 滞后两代 IP、
订阅长期指向已废弃地址而不自知。

## 2. 目标

**无人值守**地完成：定时检测 → 判定被封 → 自动换 IP → 验证新 IP 干净 → 更新订阅。

### 非目标

- 不做多节点负载/择优（当前单节点池）
- 不做告警推送（依赖 journald / 日志文件）
- 不管理 VPS 上的 Xray 服务本身（换 IP 不影响其配置）

## 3. 演进复盘：为什么最终是 API 方案

> 这一节是本项目最有价值的部分：两条路线的取舍由实测数据决定，而非预设。

### 3.1 方案一：Cookie + 网页表单（已废弃）

最初复刻控制台的 `POST instance_details.php`，携带浏览器 Cookie 提交
`action=fix_isp_blocked`。实现完成、22 项单测通过、真机验证也换 IP 成功，
但在**迁移到服务器做无人值守**时暴露致命问题：

| # | 问题 | 性质 |
| --- | --- | --- |
| 1 | **Cloud Control 会话约 30 分钟过期** | **致命** |
| 2 | 客户端丢弃所有 `Set-Cookie`，持续发送已过期的 `__cf_bm` | 严重缺陷 |
| 3 | `ensure_authenticated` 把已登录页误判为登录页 | 严重缺陷 |
| 4 | HTML 解析脆弱：区块标题、多 IPv4 候选、成功文案全靠猜 | 长期维护负担 |

问题 1 单独就足以否决该方案：**凌晨 3 点被封时，Cookie 早已失效**，
自动化无从谈起。问题 3 的诊断过程也很典型——同一请求在
「已登录 47320 字节 / 登录页 98530 字节」之间随机跳变，最初误判为
Cloudflare 按出口 IP 拦截，实测后确认是会话过期叠加 Cookie 处理缺陷。

**排除的误判**：曾担心 `cf_clearance` 绑定 IP 会导致 Cookie 无法跨机使用。
实测服务器请求返回的是正常 HTTP 200 登录页而非 Cloudflare 挑战页，
证明 **Cloudflare 未拦截该服务器**，此风险不成立。

### 3.2 方案二：Cloud API（当前方案）

Hostwinds 提供无状态 Cloud API（`api.php` + 长期 API key）。
官方文档列出 131 个 action，**其中不含 `fix_isp_blocked`**，一度以为只能退而求其次用
可能收费的 `change_main_ip`。

**关键突破**——用无损探测确认文档不完整：

发送 `action=fix_isp_blocked` 配一个**不属于本账号的 serviceid**，对比三组响应：

| 请求 | 响应 | 结论 |
| --- | --- | --- |
| 不存在的 action | `"Invalid Action."` | 未知 action 长这样 |
| `get_instance` + 错误 serviceid | `"A valid serviceid is required"` | 已知 action，卡在参数校验 |
| **`fix_isp_blocked` + 错误 serviceid** | **`"A valid serviceid is required."`** | **action 存在，仅未文档化** |

随后端到端验证成功换 IP，并在 Hostwinds 官方 **Cloud API Request Log** 中确认：
API 调用与网页点击**记录完全同形**（同命令名、同 Service ID、同 Server），
即二者是同一操作，**计费一致（免费）**。

### 3.3 方案对比

| 维度 | Cookie 方案 | **API 方案（采用）** |
| --- | --- | --- |
| 认证有效期 | **约 30 分钟** | **长期（key 有效期 1 年）** |
| 读当前 IP | HTML 解析，多候选易失败 | `get_instance` 返回 `main_ip` |
| 成功判定 | 匹配页面文案特征串 | `result == "success"` |
| 抗页面改版 | 差 | 好 |
| 无人值守 | **不可行** | 可行 |

### 3.4 决策记录

| # | 决策 | 理由 |
| --- | --- | --- |
| D1 | 用 API 而非 Cookie | 会话 30 分钟过期，无人值守不可行 |
| D2 | 用 `fix_isp_blocked` 而非 `change_main_ip` | 前者确认免费；后者疑似 $3/次 |
| D3 | 部署在订阅服务器而非 Mac | 服务器常开；Mac 会休眠导致漏检 |
| D4 | 取消 `push.sh`（scp+ssh） | 编排器就在订阅服务器上，直接改本地文件 |
| D5 | API key 绑定 IP 白名单 | key 泄露后在别处不可用 |
| D6 | 「国外可达」作为真封锁判据 | 见 §6.2，防止把未就绪误判为被封 |

## 4. 系统架构

```
systemd timer (每 30 分钟)
  └─ hostwinds_autoheal.py
       ├── Hostwinds Cloud API ── get_instance      读 main_ip
       │                       └─ fix_isp_blocked   换 IP
       ├── ipcheck.py ────────── vps234 接口         四向连通性检测
       └── 本地文件 ──────────── ~/sub-gen/ips.txt   更新节点
                              └─ ~/sub-gen/gen-sub.sh 重新生成订阅
```

关键前提：**订阅服务器（203.0.113.10）与被封节点（Hostwinds VPS）是两台机器**。
换节点 IP 不影响编排器自身的网络与订阅分发，自动化因此成立。

## 5. 接口契约

### 5.1 Hostwinds Cloud API

- 端点 `POST https://clients.hostwinds.com/cloud/api.php`（form-urlencoded）
- 认证 每请求携带 `API=<key>`，**无状态、无会话**
- key `~/hostwinds-autoheal/hostwinds.apikey`（600，绑定 IP 白名单 203.0.113.10）

| 用途 | 参数 | 返回 |
| --- | --- | --- |
| 读实例 | `action=get_instance&serviceid=<id>` | `{"success":{"main_ip":...,"status":"ACTIVE",...}}` |
| 换 IP | `action=fix_isp_blocked&serviceid=<id>&loc=ips` | `[{"result":"success","message":"Your IP is being changed!"}]` |

> ⚠️ **`get_instance` 响应含明文 root 密码 `password` 与 `configkey`。**
> 代码必须经字段白名单过滤后才可记录，原始响应绝不入日志。

### 5.2 ipcheck.py（既有工具，不修改）

`python3 ipcheck.py <IP> --json` 检测四项：国内 ICMP/TCP、国外 ICMP/TCP。

| 退出码 | 含义 |
| --- | --- |
| 0 | 四项全绿，未封锁 |
| 1 | 至少一项不通 |
| 2 | 传入 IP 非法 |
| 3 | 检测接口故障（**非判决**） |

### 5.3 订阅侧（既有，不修改）

- `~/sub-gen/ips.txt` — 节点池，`ip[:port[:uuid]]`，`#` 注释
- `~/sub-gen/gen-sub.sh` — 生成 `sub.txt`（Shadowrocket）与 `clash.yaml`

## 6. 功能需求

### 6.1 主流程

| 步骤 | 行为 | 失败处理 |
| --- | --- | --- |
| 1 | `flock` 非阻塞取锁 | 占用则静默退出 0 |
| 2 | 解析 `ips.txt` 取节点 IP 与 `:port:uuid` 后缀 | 无有效行 → 退出 2 |
| 3 | `get_instance` 读 `main_ip`，与 `ips.txt` 比对 | 不一致记 WARN，**以 Hostwinds 为准** |
| 4 | `ipcheck` 检测，按 §6.2 判定 | 见 §6.2 |
| 5 | 被封 → 换 IP 闭环（§6.3） | 见 §6.3 |
| 6 | 原子更新 `ips.txt`（仅替换 IP，保留后缀/注释/其它行），留 `.bak` | 失败 → 退出 6 |
| 7 | 执行 `gen-sub.sh` | 失败 → 退出 6，**保留已更新的 ips.txt** |

### 6.2 判定逻辑（核心）

换 IP 后 VPS 需约 10–11 分钟才真正可用，**期间四项全红**，`ipcheck` 同样返回退出码 1，
与"真被封"无法用退出码区分。若不加区分，会把"未就绪"误判为"被封"而反复空刷 IP。

因此引入**国外可达判据**：

| 条件 | 判定 | 动作 |
| --- | --- | --- |
| 退出码 0 | 未封锁 | 无动作 |
| 退出码 1 且 `outICMP`/`outTCP` **任一为真** | **真被墙**（机器对外通，仅国内不通） | 换 IP |
| 退出码 1 且国外两项**全假** | VPS 未就绪或宕机 | **不换 IP**，退出 5 |
| 退出码 1 且四项解析失败 | 保守判为被墙 | 换 IP，记 WARN |
| 退出码 1 但**国内两项均通** | 仅国外探测抖动，非真被墙（v1.3） | 无动作，判为 inconclusive |
| 退出码 3 | 非判决 | 无动作，等下次 |

### 6.3 换 IP 闭环

单轮：提交 `fix_isp_blocked` → 轮询 `get_instance` 等 `main_ip` 变化 →
**自提交时刻起**累计等满 `settle-wait` → 验证窗口内多次 `ipcheck`。

- **settle 从提交时刻计**，而非从观察到新 IP 时重新计时，避免叠加等待
- 验证需**连续 `verify-confirm` 次**判定被封才进下一轮，吸收生效时间波动
- 退出码 3/超时**不增不减**封锁计数，防止抖动导致永远凑不满阈值
- 最多 `max-cycles` 轮，受 `max-duration` 硬约束

### 6.4 幂等与并发

单轮最长可达数小时，而 timer 每 30 分钟触发。`fcntl.flock` 保证同时仅一个实例运行，
后来者立即退出 0，**防止并发重复换 IP**。

## 7. 时序参数（实测标定）

| 参数 | 默认 | 依据 |
| --- | --- | --- |
| `--poll-interval` | 20s | |
| `--ip-wait` | 300s | 实测提交→`main_ip` 变化约 **67s**，留大余量 |
| `--settle-wait` | 900s | 实测 IP 变化→网络可用约 **10–11 分钟** |
| `--verify-interval` | 120s | |
| `--verify-window` | 600s | 吸收 settle 波动 |
| `--verify-confirm` | 2 | 连续两次才判封锁 |
| `--retry-cooldown` | 30s | |
| `--submit-retries` | 3 | |
| `--max-cycles` | 5 | 免费但避免无意义空刷 |
| `--max-duration` | 10800s | 硬上限 3 小时 |
| timer 间隔 | 30min | `OnUnitActiveSec`，`Persistent=true` 补跑 |

## 8. 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 正常（未封锁 / 检测无结论 / 全链路完成 / 锁占用） |
| 1 | Hostwinds API 错误或终止性失败 |
| 2 | 参数或 `ips.txt` 解析错误 |
| 3 | 到上限仍未取得干净 IP |
| 4 | `ipcheck` 无法可靠执行 |
| 5 | VPS 疑似宕机（国外全红），已跳过换 IP |
| 6 | `ips.txt` 已更新但订阅重新生成失败 |
| 130 | 被中断 |

## 9. 安全要求

| 要求 | 实现 |
| --- | --- |
| API key 不入日志 | 仅读取不打印；单测断言日志无 key |
| root 密码不泄漏 | `safe_instance()` 字段白名单；单测断言 |
| key 最小权限 | Hostwinds 侧绑定 IP 白名单 203.0.113.10 |
| 凭证文件权限 | `hostwinds.apikey` 600 |
| 子进程无 shell | `subprocess` 参数列表；IP 先经 `IPv4Address` 校验 |
| 文件写入安全 | 临时文件 + `os.replace` 原子替换，写前留 `.bak` |

## 10. 测试

22 项 `unittest`，全离线（mock `urlopen` 与 `subprocess`），覆盖：

- 判定分支：clean / blocked / not_ready / inconclusive / fatal 全部路径
- `fix_isp_blocked` 三种响应：success / error / `Invalid Action.`
- `ips.txt`：保留后缀与注释、原子替换、生成 `.bak`、无有效行时报错
- 全链路：被封 → 换 IP → 验证 → 更新 → 重生成
- 失败路径：`gen-sub.sh` 失败保留新 IP 且退出 6；换 IP 失败不碰订阅文件
- 并发锁占用、`--dry-run` 零副作用
- **安全断言**：日志与输出中不含 API key、`password`、`configkey`
- **健壮性（v1.1）**：`get_instance` 瞬时超时应重试而非中断整轮；重试耗尽仅跳过本轮

## 11. 部署与运维

```bash
# 预演（零副作用）
python3 hostwinds_autoheal.py --dry-run

# 启用定时任务
cp hostwinds-autoheal.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now hostwinds-autoheal.timer

# 观察
systemctl list-timers hostwinds-autoheal
journalctl -u hostwinds-autoheal -f
tail -f ~/hostwinds-autoheal/autoheal.log
```

> ⚠️ **权威性**：启用后 **服务器上的 `~/sub-gen/ips.txt` 是唯一权威**。
> Mac 上的 `push.sh` 会用本地过期副本覆盖服务器版本，冲掉自动更新的 IP，
> 故已删除该脚本（归档于 `docs/history/`）。手动改节点请直接在服务器操作。

## 12. 已知限制与后续

| # | 限制 | 说明 |
| --- | --- | --- |
| L1 | ~~全链路未经真实封锁触发~~ **已解决** | 已在生产环境多次真实触发并成功换到干净 IP、更新订阅（首次 2026-09-03） |
| L2 | 时序参数为单样本 | 67s / 10–11min 各来自一次观测，已留足余量，可据日志再调 |
| L3 | Hostwinds 换 IP 冷却已观测 **已缓解(v1.2)** | 连换约 3 个后 fix 返回 success 但 IP 不再变。v1.2 降 `max-cycles` 为 2 且「提交后 IP 未变即判冷却停止本轮」，靠 30min timer 稀疏重试避开冷却 |
| L4 | API key 2027-09-27 到期 | 到期前需轮换，否则静默失效 |
| L5 | 无主动告警 | 失败仅落日志，需人工查看 |
| L6 | 单节点假设 | 只处理 `ips.txt` 第一条有效记录 |

**后续可做**：失败时推送告警（Bark/Telegram）；多节点池逐个巡检；
按日志统计封锁频率以优化巡检间隔。

## 14. 变更记录

### v1.3（2026-09-06）
- **加固**：真封锁判定新增前置条件——要求国内 `innerICMP`/`innerTCP` 至少一项确实不通，
  才可判定为「真被墙」；此前仅凭国外可达即判定，若国内全通、只是国外探测偶发抖动，
  会被误判为封锁并触发不必要的换 IP。
- 生产环境观测到该抖动的真实样本后确认修复：连续复测 3 次证实为噪声而非趋势。
- 新增 4 项单测（共 30 项）：噪声不误判、真封锁判定不变、巡检阶段噪声不触发换 IP。

### v1.2（2026-09-05）
- **观测**：Hostwinds 换 IP 存在冷却——短时间连换约 3 个后，`fix_isp_blocked` 返回 success
  但 `main_ip` 不再变化。
- **修复**：`--max-cycles` 默认 5→2；新增「提交受理但 IP 在窗口内未变 = 冷却 → 停止本次运行」，
  不再于冷却期连刷，改由 30 分钟 timer 稀疏重试。
- 新增单测（共 26 项）：冷却场景只提交一次、不连刷、不改订阅。

### v1.1（2026-09-05）
- **修复**：单次 `get_instance` 超时会中断整个换 IP 循环（EXIT_API）。
  新增 `get_instance_retry`，瞬时错误重试 `--submit-retries` 次；主流程与每轮开头均改用重试版，
  重试耗尽时仅跳过本轮而非退出。
- **验证**：L1 关闭——生产环境已真实触发自愈并成功（2026-09-03、2026-09-05）。
- 运维提醒：曾观察到 timer 被手动 `systemctl stop` 后未恢复，导致约两天无巡检。
  排查用 `systemctl is-active hostwinds-autoheal.timer` 确认。

### v1.0（2026-08-27）
- 首个上线版本：API 巡检 + 自动换 IP + 更新订阅，22 项单测，systemd timer。

## 15. 交付物

| 文件 | 说明 |
| --- | --- |
| `hostwinds_autoheal.py` | 编排器主程序（590 行，纯标准库） |
| `test_hostwinds_autoheal.py` | 22 项离线单元测试 |
| `ipcheck.py` | 封锁检测（既有，未修改） |
| `hostwinds-autoheal.service/.timer` | systemd 单元 |
| `README.md` | 运维手册 |
| `PRD.md` | 本文档 |
| `tools/hostwinds_api_probe.py` | API 探测工具（排障用） |
| `docs/history/` | 演进过程文档（Cookie 版实现未随仓库发布） |
