# 任务:把 Hostwinds 换 IP 脚本升级为"换 IP → 验证解封 → 未过再换"的自动闭环

## 涉及文件(绝对路径)
- 待改造脚本:/opt/legacy/hostwinds_fix_isp_block.py
- 验证脚本(已存在,勿改):/opt/hostwinds-fixip/ipcheck.py

## 现状
`hostwinds_fix_isp_block.py` 现在只做一件事:用已登录 Cookie 向
`POST https://clients.hostwinds.com/cloud/instance_details.php?serviceid=<id>&loc=ips`
提交表单 `action=fix_isp_blocked&loc=ips&serviceid=<id>`,发一次就结束。
它不带 CSRF token、不判断请求是否真被受理、不确认新 IP、不验证封锁是否解除。
纯标准库(argparse/urllib),无第三方依赖,需继续保持无第三方依赖、Python 3.10+、Linux 可跑。
保留现有:dry-run 默认、`--service-id/--cookie-file/--timeout/--execute`、
环境变量 `HOSTWINDS_SERVICE_ID/HOSTWINDS_COOKIE_FILE/HOSTWINDS_COOKIE/HOSTWINDS_USER_AGENT`、
不在日志泄露 Cookie。

## 目标闭环
换一次 IP 后,Hostwinds **换 IP 真正生效本身就慢,约 20 分钟**(不可干预,只能等)。
生效后调用 `ipcheck.py` 验证该 IP 是否仍被墙;未通过则再换,循环直到通过或到上限。

## ipcheck.py 契约(已实现,按此对接,不要修改它)
- 调用:`python3 /opt/hostwinds-fixip/ipcheck.py <IP> --json`
- 它检测 IP 的「国内/国外 × ICMP/TCP」四项,四项全绿才算未封锁。
- 退出码(务必按语义处理):
  - `0` = 未封锁(四项全绿)→ 闭环成功,停止
  - `1` = 被封锁(至少一项 false)→ 判为未通过
  - `2` = 传入 IP 非法(属我方 bug)→ 中止
  - `3` = 网络/接口错误(**不是判决**)→ 当"这次没测准",重试检测,绝不可当通过或封锁
- `--json` 输出形如:`[{"ip","blocked":bool,"results":{"innerICMP","innerTCP","outICMP","outTCP"},"error"}]`,把 results 四项打进日志便于观察。

## 关键陷阱(务必按此设计)
换 IP 后 VPS 尚未配置就绪时,端口未起 → TCP 为 false → ipcheck 同样返回退出码 `1`,
与"真被墙"用退出码无法区分。因此**必须先盲等 `--settle-wait`(≈20 分钟)让换 IP 生效,再开始验证**;
验证阶段还要用"窗口内多次验证 + 连续确认"吸收生效时间波动,避免刚好慢几分钟就误判封锁、白刷一轮。

## 需真机确认的两处(先留占位,给出解析样例,标 `TODO: 真机确认`)
1. 提交前 GET IP 管理页后,表单里是否有 CSRF `token`/其它隐藏字段;若有,解析出来一起 POST(不要写死 payload)。
2. 提交后成功/失败在响应体里的特征(成功串、或"冷却中/额度用尽/token 失效"等失败串);
   以及是否为"请求+Confirm"两步。占位实现要让填入特征串后即可工作。

## 详细流程

### A. 提交前
- GET `…serviceid=<id>&loc=ips`,先做会话有效性检查(现有 `ensure_authenticated` 逻辑保留:识别登录页跳转、Cloudflare/Under Attack)。
- 解析并精确取"本机当前公网 IPv4"作为旧 IP(从 IP 管理区块特定字段/DOM;现有全页正则仅作兜底)。
- `TODO: 真机确认` 解析隐藏字段(如 token)并并入 payload。

### B. 提交并判定是否被受理
- POST 表单(含 token)。解析响应体区分「受理 / 失败」,不只看 HTTP 200。
- `TODO: 真机确认` 成功/失败特征串。未受理 → 记原因,等 `--retry-cooldown` 后在本轮重试,不计为已换 IP。

### C. 轮询新 IP
- 每 `--poll-interval` 重新 GET IP 页读当前 IP,直到 `新IP != 旧IP` 或 `--ip-wait` 超时;超时视为本轮未换成功。

### D. 盲等生效(关键)
- 检测到新 IP 后 `time.sleep(--settle-wait)`(默认 1200s),等 Hostwinds 换 IP 真正生效。

### E. 验证(窗口 + 确认)
- 在 `--verify-window` 时间内,每 `--verify-interval` 调一次 ipcheck(D 之后的新 IP):
  - 退出 `0` → 闭环成功,打印新 IP + 四项结果,退出码 0。
  - 退出 `1` → 需连续 `--verify-confirm` 次才判"真封锁";期间若翻成 0 立即成功;确认封锁 → 结束本轮进入下一轮。
  - 退出 `3` → VPS 可能仍未完全就绪,继续在窗口内重试(不计入 confirm)。
  - 退出 `2` → 我方传了非法 IP,记录并以退出码 4 中止。
- 用 `subprocess` 参数列表执行,`{ip}` 先过 IPv4 校验再替换,不走 shell 拼接。
- 窗口结束仍未拿到"未封锁" → 本轮判未通过 → 下一轮。

### F. 总循环
```
记录旧 IP
for cycle in 1..--max-cycles 且 总用时 < --max-duration:
    A 提交前解析 → B 提交(未受理则 retry-cooldown 重试)
    C 轮询新 IP(超时→下一轮)
    D sleep(--settle-wait)
    E 窗口内验证
       成功 → 退出码 0
       未通过 → 下一轮(settle 已足够长,无需额外 cooldown)
超过 --max-cycles 或 --max-duration 仍未通过 → 退出码 3,打印最后 IP + 最后一次四项结果
```
每轮打印:轮次、旧 IP、新 IP、验证结果、耗时。收到 SIGINT 优雅退出并打印当前轮次与 IP。

## 命令行参数(新增,保留原有)
| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--verify-cmd` | `python3 /opt/hostwinds-fixip/ipcheck.py {ip} --json` | 验证命令模板,含 `{ip}` |
| `--settle-wait` | 1200 | 检测到新 IP 后盲等生效秒数(Hostwinds 侧约 20 分钟) |
| `--poll-interval` | 30 | 轮询新 IP 间隔秒 |
| `--ip-wait` | 300 | 单轮等新 IP 出现上限秒 |
| `--verify-interval` | 120 | 验证阶段每次间隔秒 |
| `--verify-window` | 600 | settle 后的验证窗口秒 |
| `--verify-confirm` | 2 | 判"真封锁"需连续退出 1 的次数 |
| `--retry-cooldown` | 30 | 请求未受理时重试等待秒 |
| `--max-cycles` | 6 | 最多刷新轮数 |
| `--max-duration` | 8100 | 闭环硬上限秒(≈2.25 小时) |

保留:`--service-id`、`--cookie-file`、`--timeout`、`--execute` 及对应环境变量。

## 退出码
- `0` 验证通过(拿到未封锁新 IP)
- `1` HTTP/网络/会话/Cloudflare 失败
- `2` 参数错误(serviceid 缺失或非数字、执行模式缺 cookie、缺 verify-cmd)
- `3` 到 `--max-cycles`/`--max-duration` 仍未通过
- `4` 验证脚本无法执行,或我方向其传入非法 IP(ipcheck 返回 2)

## dry-run(不带 --execute)
必须打印完整闭环计划:目标 URL、payload(不含真实 cookie)、verify-cmd、settle-wait/各间隔/上限、预计最坏时长。
**绝不发任何请求、绝不调用 ipcheck、绝不换 IP。**

## 安全/鲁棒
- 任何日志/输出不得包含 Cookie。
- 每次网络请求与子进程调用都捕获异常,单点失败进入重试/冷却而非整体崩溃。
- ipcheck 的 stdout/stderr 可透传到日志,但不得因此落入 Cookie。

## 验收标准
1. dry-run 不发请求、不调 ipcheck,打印完整闭环计划。
2. 能打印"旧 IP → 新 IP";换 IP 未生效(C 超时)不会误判成功。
3. 换到新 IP 后先盲等 `--settle-wait` 才验证;ipcheck 退出 0 才停,退出 1 需连续 `--verify-confirm` 次才判封锁,退出 3 会重试而非误判。
4. `--max-cycles`/`--max-duration` 内通过 → 退出码 0;否则 → 退出码 3 且打印最后 IP + 四项。
5. 缺 `--verify-cmd` 或(执行模式)缺 cookie → 退出码 2;ipcheck 不可执行或传入非法 IP → 退出码 4。
6. 无第三方依赖,Python 3.10+ Linux 可运行。
7. A/B 两处真机相关逻辑以 `TODO: 真机确认` 清晰标注,填入 token 字段名与成功/失败特征串后即可工作。
