# Hostwinds 节点 IP 自动巡检与修复

定时检查 Shadowrocket 节点 IP 是否被墙；被墙则通过 Hostwinds Cloud API 换 IP，
验证新 IP 干净后更新 `~/sub-gen/ips.txt` 并重新生成订阅。运行在订阅服务器上。

## ⚠️ 注意点：不要再从 本机 跑 push.sh

启用本自动化后，**服务器上的 `~/sub-gen/ips.txt` 是唯一权威**。

Mac 上的 `~/shadowrocket-sub/push.sh` 会把 **本地的 ips.txt 覆盖到服务器**，
从而把自动更新的新 IP 冲掉，订阅会退回旧的死 IP。

需要手动改节点时，直接在服务器上操作：

    vi ~/sub-gen/ips.txt && ~/sub-gen/gen-sub.sh

## 文档

- **[PRD.md](PRD.md)** — 完整产品需求文档：背景、演进复盘、架构、接口契约、判定逻辑、限制
- 本文件 — 日常运维手册
- `docs/history/` — 演进过程文档

## 组成

| 文件 | 说明 |
| --- | --- |
| `hostwinds_autoheal.py` | 编排器主程序 |
| `test_hostwinds_autoheal.py` | 离线单元测试（22 项） |
| `ipcheck.py` | 封锁检测（不修改） |
| `hostwinds.apikey` | Cloud API key，权限 600，已绑定 IP 白名单 |
| `autoheal.log` | 运行日志 |
| `PRD.md` | 产品需求文档 |
| `tools/hostwinds_api_probe.py` | Cloud API 探测工具（排障用） |

## 用法

    # 预演：只检测，绝不换 IP / 不改文件 / 不重生成订阅
    python3 hostwinds_autoheal.py --dry-run

    # 正式运行一次
    python3 hostwinds_autoheal.py

    # 单元测试
    python3 -m unittest test_hostwinds_autoheal

## 启用定时任务

    cp hostwinds-autoheal.{service,timer} /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now hostwinds-autoheal.timer

    systemctl list-timers hostwinds-autoheal    # 查看下次执行
    journalctl -u hostwinds-autoheal -n 50      # 查看运行记录
    tail -f ~/hostwinds-autoheal/autoheal.log   # 跟踪日志

停用：`systemctl disable --now hostwinds-autoheal.timer`

## 判定逻辑

`ipcheck` 返回四项：国内 ICMP/TCP、国外 ICMP/TCP。

| 情况 | 判定 | 动作 |
| --- | --- | --- |
| 四项全绿（exit 0） | 未封锁 | 无动作 |
| exit 1，国外至少一项通 | **真被墙** | 换 IP |
| exit 1，国外两项全红 | VPS 未就绪或宕机 | **不换 IP**，退出码 5 |
| exit 3 | 检测接口故障，非判决 | 无动作，等下次 |

关键点：换 IP 后 VPS 需约 10–11 分钟才真正可用，期间四项全红。
若不区分"国外可达"，会把未就绪误判成被墙而反复空刷，因此 `--settle-wait` 默认 900 秒。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 正常（未封锁 / 检测无结论 / 全链路完成） |
| 1 | Hostwinds API 错误或终止性失败 |
| 2 | 参数或 ips.txt 解析错误 |
| 3 | 到上限仍未取得干净 IP |
| 4 | ipcheck 无法可靠执行 |
| 5 | VPS 疑似宕机（国外全红），已跳过换 IP |
| 6 | ips.txt 已更新但订阅重新生成失败（需手动跑 gen-sub.sh） |
| 130 | 被中断 |

## 安全

- API key 与 `get_instance` 原始响应**绝不写入日志**。
  该响应含明文 root 密码与 configkey，代码用字段白名单过滤后才记录。
- 并发锁 `~/.hostwinds_autoheal.lock`：单轮可能跑 20+ 分钟，
  定时任务重叠时后来者直接退出，避免并发重复换 IP。

## 实测参数依据（2026-08-27）

- 提交 `fix_isp_blocked` → API `main_ip` 变化：约 67 秒（`--ip-wait 300` 留足余量）
- IP 变化 → 网络真正可用：约 10–11 分钟（`--settle-wait 900`）
- `fix_isp_blocked` 与网页 "Fix ISP Block" 按钮在 Hostwinds 日志中同形，免费
