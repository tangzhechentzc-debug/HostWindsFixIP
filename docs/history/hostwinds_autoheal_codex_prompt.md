# 任务:在订阅服务器上实现自动巡检编排器(API 版)

## 目标
在**订阅服务器 203.0.113.10**(常开 Ubuntu,Python 3.12.3)上定时检查 Shadowrocket 节点 IP
是否被墙;被墙则通过 **Hostwinds Cloud API** 换 IP,验证新 IP 干净后更新本机
`~/sub-gen/ips.txt` 并重新生成订阅。全程无需 Mac 参与、无需 cookie。

## 重要:架构已从 cookie 改为 API,以下全部作废
旧方案基于网页表单 + cookie(`hostwinds_fix_isp_block.py`)。经实测,cookie 方案不可行:
Cloud Control 会话约 30 分钟过期,无法无人值守。**不要复用该脚本的 HTML 解析、
成功特征串(SUBMIT_*_MARKERS)、ensure_authenticated、cookie 读取等任何逻辑。**
该文件可留作参考,但新实现不依赖它。

---

## 已实测确认的事实(直接作为实现依据,无需再验证)

### API 契约
- 端点:`POST https://clients.hostwinds.com/cloud/api.php`,`application/x-www-form-urlencoded`
- 认证:每次请求带 `API=<key>` 参数。**无状态,无会话,不会过期**
- key 文件:`~/hostwinds-autoheal/hostwinds.apikey`(权限 600,已就位;
  已绑定 IP 白名单 203.0.113.10,只能从本服务器调用)
- serviceid:`1234567`

**读当前 IP** —— `action=get_instance&serviceid=1234567`
返回 `{"success": {"main_ip": "...", "status": "ACTIVE", ...}}`
⚠️ **该响应包含明文 root 密码 `password` 和 `configkey`。绝对不得把原始响应写入日志。
只取需要的字段(main_ip / status),或先做字段白名单过滤再记录。**

**换 IP** —— `action=fix_isp_blocked&serviceid=1234567&loc=ips`
成功返回:`[{"result":"success","action":"Fix ISP Block","message":"Your IP is being changed!"}]`
失败返回:`[{"result":"error","action":"...","message":"...","ERROR":"..."}]`
未知 action 返回:`[{"result":"error","message":"Invalid Action.","ERROR":"6734"}]`
→ **判定成功只需 `result == "success"`,不要再做文本特征串匹配。**
已确认该命令与网页 "Fix ISP Block" 按钮在 Hostwinds 日志中同形,免费。

### 实测时序(2026-08-27 单次样本)
- 提交 → API 的 `main_ip` 变化:**约 67 秒**
- IP 变化 → VPS 网络真正可用(ipcheck 四项全绿):**约 10–11 分钟**
- settle 期间 ipcheck 持续返回 exit=1 且**四项全红**(含国外两项)

### ipcheck 契约(`~/hostwinds-autoheal/ipcheck.py`,不修改)
`python3 ipcheck.py <IP> --json` → 退出码 `0` 未封锁 / `1` 被封锁 / `2` 非法IP / `3` 接口错误(非判决)
JSON: `[{"ip","blocked","results":{"innerICMP","innerTCP","outICMP","outTCP"},"error"}]`

**关键判据(已被真实数据验证):**
- exit=1 且 `outICMP`/`outTCP` **至少一项 True** → 机器对外可达 → **真被墙**
- exit=1 且 `outICMP`/`outTCP` **全 False** → **VPS 未就绪或宕机**,不是被墙
- exit=3 → 非判决,重试

### 订阅侧(服务器本地,不修改这些脚本)
- `~/sub-gen/ips.txt` 单节点行,格式 `ip:port:uuid`
  当前:`198.51.100.20:40000:11111111-2222-3333-4444-555555555555`
- `~/sub-gen/gen-sub.sh` 读 ips.txt 重新生成订阅(直接执行即可,**不需要 scp/ssh/push.sh**)
- `~/sub-gen/gen-sub.env` 含 SUB_TOKEN,**不得读取或打印**

---

## 实现:新增 `~/hostwinds-autoheal/hostwinds_autoheal.py`
纯标准库,Python 3.10+。建议内部分两层:轻量 API 客户端函数 + 编排流程。

### 流程
1. **取锁** `fcntl.flock` 非阻塞(默认 `~/.hostwinds_autoheal.lock`)。拿不到 → 记一行日志,
   **退出码 0**(上一轮仍在跑,单轮可能 20+ 分钟)。硬要求,防并发重复换 IP。
2. **读节点 IP**:解析 `~/sub-gen/ips.txt`,跳过空行与 `#`,取第一条有效记录的 IP,
   记住 `:port:uuid` 尾巴。解析不到 → 退出码 2。
   同时用 `get_instance` 读 Hostwinds 侧 `main_ip`;**若两者不一致**,记 WARNING
   (说明有人手动改过),**以 Hostwinds 侧为准**继续。
3. **检测**该 IP:按上面「关键判据」分支
   - 未封锁 → 无动作,记一行日志,**退出码 0**(最常见路径,保持安静)
   - 真被墙 → 进入第 4 步
   - 国外全红 → 不换 IP,WARNING,**退出码 5**(VPS 疑似宕机,换了也没用)
   - exit=3 → 无动作,退出码 0,等下次巡检
   - exit=2/其它 → 退出码 4
4. **换 IP 闭环**(最多 `--max-cycles` 轮,受 `--max-duration` 硬约束):
   a. 记录 `old_ip`(来自 get_instance),提交 `fix_isp_blocked`
      - `result != "success"` → 记录 message;可重试类(限频/冷却语义)按 `--retry-cooldown`
        重试至 `--submit-retries` 次,耗尽则本轮失败进下一轮;其它错误 → 退出码 1
   b. 每 `--poll-interval` 调 `get_instance`,等 `main_ip != old_ip`,上限 `--ip-wait`;
      超时 → 本轮失败进下一轮
   c. 从**提交时刻**起累计等满 `--settle-wait`(不足则补足;已超过则直接进 d)
   d. 在 `--verify-window` 内每 `--verify-interval` 调一次 ipcheck:
      - exit=0 → 成功,进入第 5 步
      - exit=1 且国外可达 → 累计封锁计数,达 `--verify-confirm` 次 → 本轮判被墙,进下一轮
      - exit=1 且国外全红 → inconclusive,**不增不减**计数,窗口内继续
      - exit=3/超时 → inconclusive,同上
      - exit=2/无法执行 → 退出码 4
      - 窗口结束无结论 → 本轮失败进下一轮
   e. 轮次或总时长耗尽仍无干净 IP → **退出码 3**,日志写明最后 IP 与最后一次四项结果;
      若已换到新 IP 但未及验证,输出须明确「已换到新 IP <ip>,但未及验证」
5. **更新 `~/sub-gen/ips.txt`**:备份为 `ips.txt.bak`;**只替换目标行的 IP 部分**,
   原样保留 `:port:uuid` 及其它所有行(注释、空行、其它节点);原子写(临时文件 → `os.replace`)。
   新旧 IP 相同 → 跳过,warning,退出码 0。
6. **重新生成订阅**:执行 `~/sub-gen/gen-sub.sh`(`cwd=~/sub-gen`),透传输出。
   成功 → 记录"订阅已更新",**退出码 0**;
   失败 → ERROR,**保留已更新的 ips.txt**(IP 确实变了,回滚只会更错),**退出码 6**。

### 命令行参数(默认值已按实测调优)
| 参数 | 默认 | 依据 |
| --- | --- | --- |
| `--service-id` | 1234567 | |
| `--api-key-file` | `~/hostwinds-autoheal/hostwinds.apikey` | |
| `--ips-file` | `~/sub-gen/ips.txt` | |
| `--gen-sub` | `~/sub-gen/gen-sub.sh` | |
| `--ipcheck` | `~/hostwinds-autoheal/ipcheck.py` | |
| `--poll-interval` | 20 | 实测 67s 反映新 IP |
| `--ip-wait` | 300 | 实测 67s,留大余量 |
| `--settle-wait` | 900 | **实测 10–11 分钟**,留 4–5 分钟余量 |
| `--verify-interval` | 120 | |
| `--verify-window` | 600 | 吸收 settle 波动 |
| `--verify-confirm` | 2 | |
| `--retry-cooldown` | 30 | |
| `--submit-retries` | 3 | |
| `--max-cycles` | 5 | fix 免费,但避免无意义空刷 |
| `--max-duration` | 10800 | 5 轮 × 约 30 分钟 |
| `--timeout` | 30 | 单次 HTTP 超时 |
| `--lock-file` | `~/.hostwinds_autoheal.lock` | |
| `--log-file` | `~/hostwinds-autoheal/autoheal.log` | |
| `--dry-run` | flag | 只检测并打印计划,**绝不换 IP / 不改 ips.txt / 不重生成订阅** |

### 退出码
`0` 正常(未封锁 / 检测不准 / 全链路完成) · `1` API 错误或终止性失败 · `2` 参数或 ips.txt 解析错误 ·
`3` 到上限仍未拿到干净 IP · `4` ipcheck 故障 · `5` VPS 疑似宕机(国外全红,已跳过换 IP) ·
`6` ips.txt 已更新但订阅重生成失败 · `130` 中断。

### 日志与安全
- 追加带时间戳的行:检测四项、判定依据(国外可达与否)、是否换 IP、旧→新 IP、
  每轮耗时、订阅重生成结果。**每次换 IP 都要记录时间戳,便于日后对账单。**
- **绝不输出**:API key、`get_instance` 原始响应(含 password/configkey)、SUB_TOKEN、gen-sub.env 内容。
  实现一个字段白名单函数,只允许 main_ip/status/serviceid 等安全字段进日志。

## systemd timer
生成 `hostwinds-autoheal.service`(Type=oneshot)+ `hostwinds-autoheal.timer`
(`OnBootSec=5min`,`OnUnitActiveSec=30min`,`Persistent=true`),以 root 运行
(gen-sub.sh 需写 `/var/www/sub`)。**不要自动 enable**,在 README 给出:
`systemctl enable --now hostwinds-autoheal.timer`。

## ⚠️ README 必须显眼写明
自动化启用后,**服务器上的 `~/sub-gen/ips.txt` 是唯一权威**。
Mac 上 `~/shadowrocket-sub/push.sh` 会用 Mac 本地(过期的)ips.txt
**覆盖服务器版本**,把自动更新的新 IP 冲掉。**启用后不要再从 Mac 跑 push.sh**;
需手动干预就直接在服务器改 ips.txt 再跑 gen-sub.sh。

## 测试(unittest,全离线,mock urlopen 与 subprocess)
- 未封锁 → 不换 IP、不改文件、不重生成,退出码 0
- 真被墙(国外可达)→ 触发全链路
- 国外全红 → 不换 IP,退出码 5
- ipcheck exit=3 → 无动作,退出码 0
- `fix_isp_blocked` 返回 success / error / "Invalid Action." 三种分支
- IP 变化轮询:按时超时 → 下一轮;settle 从**提交时刻**计,提前变化要补足
- ips.txt 更新:保留 `:port:uuid`、保留注释与其它行、原子替换、生成 .bak
- 换 IP 失败 → 不碰 ips.txt、不调 gen-sub.sh
- gen-sub.sh 失败 → 退出码 6 且 ips.txt 保持已更新
- 锁被占用 → 立即退出码 0 且无任何动作
- `--dry-run` 不换 IP、不写文件、不重生成
- **断言日志中不含 API key,也不含 get_instance 的 password/configkey 字段**
最后跑 `unittest`、`py_compile`、`--help`、`--dry-run`。

## 注意
- 不修改 `ipcheck.py`、`gen-sub.sh`、`gen-sub.env`、`clash-rules.txt`。
- 交付后由用户先手动 `--dry-run`,再启用 timer。
