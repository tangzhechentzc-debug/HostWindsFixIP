# 阶段四:端到端跑通验证半场 + 一处基于真机数据的加固

> 背景:真机已确认提交半场可用(无 CSRF token;成功特征 `Fix ISP Block Succeeded` /
> `Your IP is being changed!`;POST 302→200;无二次 Confirm;IP 已从
> 198.51.100.9 换到 198.51.100.16,页面反映约 4 分 39 秒)。
> 但换到的新 IP 经 ipcheck 实测仍被墙:outICMP=true, outTCP=true,
> innerICMP=false, innerTCP=false(exit=1),即"国外通、国内不通 = 真被 GFW 封"。
> 本阶段把验证半场真正跑通,并据此数据加固判定逻辑。

## 运行环境:就在当前 macOS 跑,不要挪 Linux
`ipcheck.py` 只调用 vps234.com 的 web 接口,和被测 VPS 无关,任何有网的机器都能跑。
因此完整闭环(提交 + 验证 + 再刷新)直接在当前 macOS 环境跑,`--verify-cmd` 用默认路径
`python3 /opt/hostwinds-fixip/ipcheck.py {ip} --json` 即可,不需要任何 Linux 路径。
阶段四不缺任何外部输入,可直接执行。

## 加固:用"国外两项"区分"VPS 未就绪"和"真被墙"(改 verify_ip)
在 `verify_ip` 里,对 `ipcheck` 退出码为 1(kind="blocked")的这次结果,读取 `attempt.results`:
- 若 `outICMP` 和 `outTCP` 至少一项为 True(机器对外可达)→ 视为**真封锁**,按现逻辑累计
  `blocked_count`,达到 `verify-confirm` 后判本轮封锁、进入下一轮。
- 若 `outICMP` 与 `outTCP` **都为 False**(对外都不通,VPS 很可能仍在配置/未就绪)→ 视为
  **inconclusive**,**不累计也不清零** `blocked_count`,窗口内继续重试;等同于退出码 3 的处理。
- `results` 缺失/无法解析(四项拿不到)时,保守起见退回**现有行为**:仍按真封锁累计
  (避免因解析失败而永远无法确认封锁);仅打一条 warning。
- 退出码 0(成功)和退出码 3/超时的处理保持不变。

把用于判定的字段名做成模块级常量,例如 `REACHABILITY_FIELDS = ("outICMP", "outTCP")`,便于日后调整。
日志中打印该次判定依据,例如:`国外可达=True → 计为真封锁 2/2` 或 `国外不可达 → 判 inconclusive,不计数`。

## 测试补充(unittest,离线)
- 国外绿+国内红 的 exit=1 → 累计封锁,达到阈值判 blocked。
- 国外全红 的 exit=1 → 不累计、不清零,按 inconclusive 继续。
- results 缺失/JSON 损坏 的 exit=1 → 退回旧行为仍累计,并记录 warning。
- 与既有 `1→3→1`、退出 0 成功、退出 2/未知码 fatal 等用例并存不冲突。

## 端到端实跑(真实执行,注意这会真的换 IP)
配好 cookie 后,在 macOS 跑一次真实 `--execute`,确认:提交命中成功特征 → 观察到新 IP →
settle → 调 ipcheck → 按新逻辑判 blocked 并进入下一轮 / 或判 success 退出。
真实换 IP 会消耗 Hostwinds 额度且每轮约 20 分钟,首次可把 `--max-cycles 1`、
`--settle-wait` 临时调小做冒烟,再用默认值正式跑。冒烟后务必把 `--settle-wait` 还原到 1200,
否则会在 VPS 未就绪时误判。

## 保持不变
成功特征安全门、退出码 0–4/130、Cookie 脱敏、settle 从提交时刻计、
merged IP 轮询窗口、dry-run 约束全部保留。

## 仍未确认(留待真机)
- Hostwinds fix_isp_blocked 的换 IP 额度上限与冷却/限频时长(真机尚未测出,勿一次刷太多轮)。
- VPS 换 IP 后"对外可达(国外变绿)"距提交需多久——决定 settle-wait 能否下调,当前保守取 1200。
