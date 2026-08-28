# 决策记录:换 IP 走 cookie + Fix ISP Block,不走 Cloud API

日期:2026-08-27

## 决定
Hostwinds 自动换 IP 工具**维持 cookie + 网页表单 `fix_isp_blocked` 的方案**,
**不改用 Hostwinds Cloud API**。验证半场继续用 `ipcheck.py`。

## 依据
- 实际有效的换 IP 操作是控制台里的 **"Fix ISP Block"** 按钮(不是 "Change Main IP")。
- 通读 Hostwinds Cloud API 文档(https://developers.hostwinds.com/cloud/,端点
  `https://clients.hostwinds.com/cloud/api.php`,131 个 action)后确认:
  - 关键词 `fix_isp` / `fix isp` / `blocked` / `block` 命中数全为 0。
  - **API 没有 "Fix ISP Block" 对应的 action。**
  - API 的 `change_main_ip` 是控制台里另一个独立按钮 "⇄ Change Main IP",与 Fix ISP Block
    是两个不同操作;`repair_instance` 文档仅"repair an instance",与换 IP 无关,不能替代。
- 因此 Fix ISP Block **只能走网页面板**,没有 API/token 路径。

## 由此确认的几点
- "为什么不用账号密码 / API key":连官方 API 都做不了 Fix ISP Block,此操作没有 token 路径;
  cookie 是唯一可行且相对安全(会过期、泄露损失可控、不碰明文密码)的方式。
- 缓解 cookie 过期:登录时勾"记住我"取长效 cookie。
- 换 IP 有费用风险(网页换主 IP 记为 $3/次,API 文档另有 `create_ip_cleanup_fee`);
  正式循环前需确认 Fix ISP Block 是否计费,再定 `--max-cycles`。

## 可选(暂不采用)
- 只把"读当前 IP"从 HTML 抓取换成 API `get_instance`(返回 `main_ip`)。
  代价是要同时管 cookie + API key 两套凭证。当前倾向保持单一 cookie 凭证,不引入 API key,
  除非 HTML 解析在真机上不可靠。

## 现状
- cookie 版脚本与测试(Codex 已实现)继续推进。
- 阶段四(验证半场端到端 + "国外可达才信封锁"加固)照常执行,见
  hostwinds_phase4_verify_codex_prompt.md。
