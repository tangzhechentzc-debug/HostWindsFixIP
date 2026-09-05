#!/usr/bin/env python3
"""Hostwinds 节点 IP 自动巡检与修复编排器（Cloud API 版）。

流程：读 ips.txt 取节点 IP → ipcheck 检测 → 被墙则 fix_isp_blocked 换 IP
      → 等生效 → 验证干净 → 更新 ips.txt → 重新生成订阅。

安全：API key 与 get_instance 原始响应（含明文 root 密码）绝不入日志。
"""

import argparse
import fcntl
import ipaddress
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_URL = "https://clients.hostwinds.com/cloud/api.php"
DEFAULT_SERVICE_ID = os.environ.get("HOSTWINDS_SERVICE_ID", "")

RESULT_FIELDS = ("innerICMP", "innerTCP", "outICMP", "outTCP")
# 国外可达判据：这两项任一为真 = 机器对外通，exit=1 才是真被墙
REACHABILITY_FIELDS = ("outICMP", "outTCP")
# 允许进日志的 get_instance 字段白名单（其余字段含密码等敏感信息）
SAFE_INSTANCE_FIELDS = ("serviceid", "main_ip", "status", "srvrname", "hostname")

# 默认时序（依据 2026-08-27 实测：提交→IP变化 约 67s；IP变化→网络可用 约 10-11 分钟）
POLL_INTERVAL = 20.0
IP_WAIT = 300.0
SETTLE_WAIT = 900.0
VERIFY_INTERVAL = 120.0
VERIFY_WINDOW = 600.0
VERIFY_CONFIRM = 2
RETRY_COOLDOWN = 30.0
SUBMIT_RETRIES = 3
MAX_CYCLES = 2
MAX_DURATION = 10800.0
HTTP_TIMEOUT = 30.0

# 提交失败时可重试的语义（限频/冷却）
RETRYABLE_MARKERS = (
    "cooldown", "rate limit", "too many requests", "try again", "please wait",
)

EXIT_OK = 0
EXIT_API = 1
EXIT_ARGS = 2
EXIT_EXHAUSTED = 3
EXIT_IPCHECK = 4
EXIT_VPS_DOWN = 5
EXIT_GENSUB = 6
EXIT_INTERRUPT = 130


class ApiError(RuntimeError):
    pass


@dataclass
class Check:
    """一次 ipcheck 的判定结果。kind: clean/blocked/not_ready/inconclusive/fatal"""
    kind: str
    returncode: int | None
    results: dict | None
    warning: str | None = None


@dataclass
class NodeLine:
    index: int          # 在 lines 中的下标
    ip: str
    suffix: str         # ":port:uuid" 或 ""
    lines: list[str]


# --------------------------------------------------------------------------- 日志

class Logger:
    def __init__(self, path: pathlib.Path | None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message: str, level: str = "INFO") -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {level:<5} {message}"
        print(line, flush=True)
        if self.path:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass  # 日志写不进去不应中断主流程


def safe_instance(data: dict) -> dict:
    """只保留白名单字段，防止 password/configkey 等泄漏到日志。"""
    return {k: data.get(k) for k in SAFE_INSTANCE_FIELDS if k in data}


# --------------------------------------------------------------------------- API

def api_call(action: str, key: str, timeout: float, **params):
    """调用 Cloud API，返回已解析的 JSON。失败抛 ApiError。"""
    body = urlencode({"action": action, "API": key, **params}).encode()
    req = Request(API_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "hostwinds-autoheal/1.0",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        raise ApiError(f"HTTP {exc.code}") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise ApiError(f"{type(exc).__name__}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f"响应不是 JSON：{exc}") from exc


def get_instance(key: str, service_id: str, timeout: float) -> dict:
    """返回白名单过滤后的实例信息。"""
    parsed = api_call("get_instance", key, timeout, serviceid=service_id)
    if isinstance(parsed, dict) and isinstance(parsed.get("success"), dict):
        return safe_instance(parsed["success"])
    msg = ""
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        msg = str(parsed[0].get("message", ""))
    raise ApiError(f"get_instance 未返回实例数据：{msg or '未知错误'}")


def get_instance_retry(key: str, service_id: str, timeout: float,
                       attempts: int, cooldown: float, log=None) -> dict:
    """带重试的 get_instance：瞬时错误不应中断整个自愈循环。"""
    last = None
    for i in range(1, attempts + 1):
        try:
            return get_instance(key, service_id, timeout)
        except ApiError as exc:
            last = exc
            if log:
                log(f"读取实例失败：{exc}（{i}/{attempts}）", "WARN")
            if i < attempts:
                time.sleep(cooldown)
    raise last if last else ApiError("get_instance 未知失败")


def submit_fix(key: str, service_id: str, timeout: float) -> tuple[bool, str]:
    """提交 fix_isp_blocked。返回 (是否受理, 消息)。"""
    parsed = api_call("fix_isp_blocked", key, timeout, serviceid=service_id, loc="ips")
    items = parsed if isinstance(parsed, list) else [parsed]
    for item in items:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message", ""))
        if str(item.get("result", "")).lower() == "success":
            return True, message
        return False, message or "未知错误"
    return False, "响应为空"


def is_retryable(message: str) -> bool:
    low = message.casefold()
    return any(marker in low for marker in RETRYABLE_MARKERS)


# --------------------------------------------------------------------------- ipcheck

def run_ipcheck(ipcheck: pathlib.Path, ip: str, timeout: float) -> Check:
    try:
        ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError:
        return Check("fatal", 2, None, f"非法 IPv4：{ip}")

    cmd = [sys.executable, str(ipcheck), ip, "--json"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return Check("inconclusive", None, None, "ipcheck 执行超时")
    except OSError as exc:
        return Check("fatal", None, None, f"无法启动 ipcheck：{exc}")

    results, warning = parse_ipcheck_json(done.stdout, ip)
    rc = done.returncode
    if rc == 0:
        return Check("clean", 0, results, warning)
    if rc == 3:
        return Check("inconclusive", 3, results, warning or "ipcheck 接口错误")
    if rc == 2:
        return Check("fatal", 2, results, warning or "ipcheck 拒绝了传入的 IP")
    if rc != 1:
        return Check("fatal", rc, results, warning or f"ipcheck 未知退出码 {rc}")

    # rc == 1：区分「真被墙」与「VPS 未就绪」
    complete = results is not None and all(
        isinstance(results.get(f), bool) for f in RESULT_FIELDS
    )
    if not complete:
        return Check("blocked", 1, results,
                     (warning or "") + "；四项结果不完整，保守判为被墙")
    if any(results.get(f) is True for f in REACHABILITY_FIELDS):
        return Check("blocked", 1, results, warning)
    return Check("not_ready", 1, results, warning)


def parse_ipcheck_json(stdout: str, expected_ip: str) -> tuple[dict | None, str | None]:
    try:
        value = json.loads(stdout)
        if not isinstance(value, list) or not value or not isinstance(value[0], dict):
            raise ValueError("顶层不是非空对象数组")
        item = value[0]
        warnings = []
        if item.get("ip") != expected_ip:
            warnings.append("JSON 中的 IP 与目标不一致")
        raw = item.get("results")
        if not isinstance(raw, dict):
            raise ValueError("缺少 results 对象")
        results = {f: raw.get(f) for f in RESULT_FIELDS}
        return results, "；".join(warnings) or None
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return None, f"无法解析 ipcheck JSON：{exc}"


def format_results(results: dict | None) -> str:
    if not results:
        return " ".join(f"{f}=?" for f in RESULT_FIELDS)
    return " ".join(f"{f}={results.get(f)}" for f in RESULT_FIELDS)


# --------------------------------------------------------------------------- ips.txt

def parse_ips_file(path: pathlib.Path) -> NodeLine:
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, raw in enumerate(lines):
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            continue
        parts = stripped.split(":")
        try:
            ipaddress.IPv4Address(parts[0])
        except ipaddress.AddressValueError:
            continue
        suffix = "" if len(parts) == 1 else ":" + ":".join(parts[1:])
        return NodeLine(index, parts[0], suffix, lines)
    raise ValueError(f"{path} 中没有有效的节点行")


def update_ips_file(path: pathlib.Path, node: NodeLine, new_ip: str) -> None:
    """只替换目标行的 IP，保留后缀与其它所有行；原子写并留 .bak。"""
    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text("\n".join(node.lines) + "\n", encoding="utf-8")

    lines = list(node.lines)
    lines[node.index] = new_ip + node.suffix
    payload = "\n".join(lines) + "\n"

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ips-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def regenerate_subscription(gen_sub: pathlib.Path, log: Logger) -> bool:
    try:
        done = subprocess.run([str(gen_sub)], cwd=str(gen_sub.parent),
                              capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"执行 {gen_sub} 失败：{type(exc).__name__}", "ERROR")
        return False
    for line in (done.stdout or "").splitlines():
        log(f"  gen-sub: {line}")
    for line in (done.stderr or "").splitlines():
        log(f"  gen-sub[err]: {line}", "WARN")
    return done.returncode == 0


# --------------------------------------------------------------------------- 时间

def sleep_until(target: float, deadline: float) -> bool:
    """睡到 target，但不越过 deadline。返回是否仍在 deadline 之内。"""
    while True:
        now = time.monotonic()
        if now >= deadline:
            return False
        if now >= target:
            return True
        time.sleep(min(1.0, target - now, deadline - now))


# --------------------------------------------------------------------------- 换 IP 闭环

def verify_new_ip(args, ip: str, log: Logger, deadline: float) -> str:
    """在验证窗口内判定新 IP。返回 clean/blocked/fatal/inconclusive。"""
    window_end = min(time.monotonic() + args.verify_window, deadline)
    blocked_count = 0
    while time.monotonic() < window_end:
        started = time.monotonic()
        budget = max(5.0, min(args.verify_interval, window_end - started))
        check = run_ipcheck(args.ipcheck, ip, budget)
        log(f"  ipcheck exit={check.returncode} {format_results(check.results)}")
        if check.warning:
            log(f"  warning：{check.warning}", "WARN")

        if check.kind == "clean":
            return "clean"
        if check.kind == "fatal":
            return "fatal"
        if check.kind == "blocked":
            blocked_count += 1
            log(f"  国外可达 → 计为真封锁（{blocked_count}/{args.verify_confirm}）")
            if blocked_count >= args.verify_confirm:
                return "blocked"
        elif check.kind == "not_ready":
            log("  国外不可达 → VPS 尚未就绪，不计数")
        else:
            log("  检测无结论，不计数")

        if not sleep_until(started + args.verify_interval, window_end):
            break
    return "inconclusive"


def fix_loop(args, key: str, log: Logger, deadline: float) -> tuple[str, str | None]:
    """换 IP 主循环。返回 (状态, 新IP)。状态: clean/exhausted/api_error/ipcheck_error/unverified"""
    last_new_ip = None
    for cycle in range(1, args.max_cycles + 1):
        if time.monotonic() >= deadline:
            break
        log(f"—— 第 {cycle}/{args.max_cycles} 轮 ——")

        try:
            old_ip = get_instance_retry(
                key, args.service_id, args.timeout,
                args.submit_retries, args.retry_cooldown, log,
            ).get("main_ip")
        except ApiError as exc:
            log(f"多次重试后仍读不到当前 IP：{exc}，进入下一轮", "WARN")
            continue
        log(f"提交前 main_ip = {old_ip}")

        submitted_at = None
        accepted = False
        for attempt in range(1, args.submit_retries + 1):
            if time.monotonic() >= deadline:
                break
            submitted_at = time.monotonic()
            try:
                ok, message = submit_fix(key, args.service_id, args.timeout)
            except ApiError as exc:
                log(f"提交异常：{exc}（{attempt}/{args.submit_retries}）", "WARN")
                if attempt == args.submit_retries:
                    break
                sleep_until(time.monotonic() + args.retry_cooldown, deadline)
                continue
            if ok:
                log(f"已受理：{message}")
                accepted = True
                break
            if is_retryable(message):
                log(f"可重试失败：{message}（{attempt}/{args.submit_retries}）", "WARN")
                if attempt == args.submit_retries:
                    break
                sleep_until(time.monotonic() + args.retry_cooldown, deadline)
                continue
            log(f"提交被拒（终止性）：{message}", "ERROR")
            return "api_error", last_new_ip

        if not accepted:
            log("本轮提交未受理，进入下一轮", "WARN")
            continue

        # 等 main_ip 变化
        ip_deadline = min(submitted_at + args.ip_wait, deadline)
        new_ip = None
        while time.monotonic() < ip_deadline:
            if not sleep_until(time.monotonic() + args.poll_interval, ip_deadline):
                break
            try:
                current = get_instance(key, args.service_id, args.timeout).get("main_ip")
            except ApiError as exc:
                log(f"  轮询失败：{exc}", "WARN")
                continue
            if current and current != old_ip:
                new_ip = current
                break
            log(f"  main_ip 仍为 {current}")
        if not new_ip:
            # 提交已受理但 IP 始终不变 = Hostwinds 换 IP 冷却/限频。
            # 继续连刷只会撞在冷却上，直接结束本次运行，靠 timer 稀疏重试。
            log("提交已受理但 IP 未变化，疑似换 IP 冷却；停止本次运行，等待下次巡检", "WARN")
            return "cooldown", last_new_ip

        last_new_ip = new_ip
        log(f"IP 已变化：{old_ip} → {new_ip}")

        # settle 从提交时刻起累计
        settle_due = submitted_at + args.settle_wait
        if time.monotonic() < settle_due:
            if settle_due > deadline:
                log(f"已换到新 IP {new_ip}，但未及验证", "WARN")
                return "unverified", new_ip
            remain = int(settle_due - time.monotonic())
            log(f"等待网络生效，还需 {remain} 秒（自提交起累计 {args.settle_wait:g}s）")
            sleep_until(settle_due, deadline)
        if time.monotonic() >= deadline:
            log(f"已换到新 IP {new_ip}，但未及验证", "WARN")
            return "unverified", new_ip

        outcome = verify_new_ip(args, new_ip, log, deadline)
        if outcome == "clean":
            log(f"验证通过：{new_ip} 未被封锁")
            return "clean", new_ip
        if outcome == "fatal":
            return "ipcheck_error", new_ip
        log(f"{new_ip} 未通过本轮验证（{outcome}），进入下一轮", "WARN")

    return "exhausted", last_new_ip


# --------------------------------------------------------------------------- 主流程

def build_parser() -> argparse.ArgumentParser:
    home = pathlib.Path.home()
    base = home / "hostwinds-autoheal"
    sub = home / "sub-gen"
    p = argparse.ArgumentParser(description="Hostwinds 节点 IP 自动巡检与修复（API 版）")
    p.add_argument("--service-id", default=DEFAULT_SERVICE_ID,
                   help="Hostwinds 服务 ID，也可用环境变量 HOSTWINDS_SERVICE_ID")
    p.add_argument("--api-key-file", type=pathlib.Path, default=base / "hostwinds.apikey")
    p.add_argument("--ips-file", type=pathlib.Path, default=sub / "ips.txt")
    p.add_argument("--gen-sub", type=pathlib.Path, default=sub / "gen-sub.sh")
    p.add_argument("--ipcheck", type=pathlib.Path, default=base / "ipcheck.py")
    p.add_argument("--poll-interval", type=float, default=POLL_INTERVAL)
    p.add_argument("--ip-wait", type=float, default=IP_WAIT)
    p.add_argument("--settle-wait", type=float, default=SETTLE_WAIT)
    p.add_argument("--verify-interval", type=float, default=VERIFY_INTERVAL)
    p.add_argument("--verify-window", type=float, default=VERIFY_WINDOW)
    p.add_argument("--verify-confirm", type=int, default=VERIFY_CONFIRM)
    p.add_argument("--retry-cooldown", type=float, default=RETRY_COOLDOWN)
    p.add_argument("--submit-retries", type=int, default=SUBMIT_RETRIES)
    p.add_argument("--max-cycles", type=int, default=MAX_CYCLES)
    p.add_argument("--max-duration", type=float, default=MAX_DURATION)
    p.add_argument("--timeout", type=float, default=HTTP_TIMEOUT)
    p.add_argument("--lock-file", type=pathlib.Path, default=home / ".hostwinds_autoheal.lock")
    p.add_argument("--log-file", type=pathlib.Path, default=base / "autoheal.log")
    p.add_argument("--dry-run", action="store_true",
                   help="只检测并打印计划；绝不换 IP、不改 ips.txt、不重生成订阅")
    return p


def validate(args) -> str | None:
    if not args.service_id:
        return "缺少服务 ID：用 --service-id 指定，或设置环境变量 HOSTWINDS_SERVICE_ID"
    if not str(args.service_id).isdigit():
        return "--service-id 必须是数字"
    for name in ("poll_interval", "ip_wait", "verify_interval", "verify_window",
                 "max_duration", "timeout"):
        if getattr(args, name) <= 0:
            return f"--{name.replace('_', '-')} 必须大于 0"
    if args.settle_wait < 0 or args.retry_cooldown < 0:
        return "--settle-wait / --retry-cooldown 不能为负"
    for name in ("verify_confirm", "submit_retries", "max_cycles"):
        if getattr(args, name) < 1:
            return f"--{name.replace('_', '-')} 必须 >= 1"
    if not args.ips_file.is_file():
        return f"找不到 {args.ips_file}"
    if not args.ipcheck.is_file():
        return f"找不到 {args.ipcheck}"
    if not args.dry_run and not args.gen_sub.is_file():
        return f"找不到 {args.gen_sub}"
    return None


def acquire_lock(path: pathlib.Path):
    handle = path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    error = validate(args)
    if error:
        print(f"错误：{error}", file=sys.stderr)
        return EXIT_ARGS

    log = Logger(args.log_file)

    lock = acquire_lock(args.lock_file)
    if lock is None:
        log("已有实例在运行（锁被占用），本次跳过")
        return EXIT_OK

    started = time.monotonic()
    deadline = started + args.max_duration
    try:
        try:
            node = parse_ips_file(args.ips_file)
        except (OSError, ValueError) as exc:
            log(f"解析 {args.ips_file} 失败：{exc}", "ERROR")
            return EXIT_ARGS

        try:
            key = args.api_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log(f"读取 API key 失败：{exc}", "ERROR")
            return EXIT_ARGS
        if not key:
            log("API key 文件为空", "ERROR")
            return EXIT_ARGS

        try:
            info = get_instance_retry(
                key, args.service_id, args.timeout,
                args.submit_retries, args.retry_cooldown, log,
            )
        except ApiError as exc:
            log(f"多次重试后仍无法调用 get_instance：{exc}", "ERROR")
            return EXIT_API
        main_ip = info.get("main_ip")
        log(f"Hostwinds main_ip={main_ip} status={info.get('status')}；"
            f"ips.txt 节点={node.ip}")
        if main_ip and main_ip != node.ip:
            log("ips.txt 与 Hostwinds 不一致（可能有人手动改过），以 Hostwinds 为准", "WARN")
        target_ip = main_ip or node.ip

        check = run_ipcheck(args.ipcheck, target_ip, args.verify_interval)
        log(f"检测 {target_ip}：exit={check.returncode} {format_results(check.results)}")
        if check.warning:
            log(f"warning：{check.warning}", "WARN")

        if check.kind == "clean":
            log("未被封锁，无需处理")
            if main_ip and main_ip != node.ip and not args.dry_run:
                log("但 ips.txt 落后于 Hostwinds，同步并重新生成订阅")
                update_ips_file(args.ips_file, node, main_ip)
                if not regenerate_subscription(args.gen_sub, log):
                    return EXIT_GENSUB
                log("订阅已更新")
            return EXIT_OK
        if check.kind == "not_ready":
            log("国外两项均不可达：VPS 疑似宕机或未就绪，不换 IP", "WARN")
            return EXIT_VPS_DOWN
        if check.kind == "inconclusive":
            log("检测无结论，本次不动作，等待下次巡检")
            return EXIT_OK
        if check.kind == "fatal":
            log("ipcheck 无法可靠执行", "ERROR")
            return EXIT_IPCHECK

        # kind == blocked
        log(f"{target_ip} 确认被封锁，开始换 IP")
        if args.dry_run:
            log("[dry-run] 将执行：fix_isp_blocked → 等待生效 → 验证 → "
                f"更新 {args.ips_file} → 执行 {args.gen_sub}（本次不执行）")
            return EXIT_OK

        outcome, new_ip = fix_loop(args, key, log, deadline)
        if outcome == "api_error":
            return EXIT_API
        if outcome == "ipcheck_error":
            return EXIT_IPCHECK
        if outcome == "cooldown":
            log("因换 IP 冷却本次未能取得干净 IP，等待下次 timer 稀疏重试", "WARN")
            return EXIT_EXHAUSTED
        if outcome in ("exhausted", "unverified"):
            log(f"未能在上限内取得干净 IP；最后 IP={new_ip or target_ip}", "ERROR")
            return EXIT_EXHAUSTED

        if new_ip == node.ip:
            log("新 IP 与 ips.txt 相同，无需更新", "WARN")
            return EXIT_OK

        try:
            node = parse_ips_file(args.ips_file)  # 重读，避免期间被改动
            update_ips_file(args.ips_file, node, new_ip)
        except (OSError, ValueError) as exc:
            log(f"更新 {args.ips_file} 失败：{exc}", "ERROR")
            return EXIT_GENSUB
        log(f"ips.txt 已更新：{node.ip} → {new_ip}")

        if not regenerate_subscription(args.gen_sub, log):
            log("订阅重新生成失败；ips.txt 已是新 IP，请手动执行 gen-sub.sh", "ERROR")
            return EXIT_GENSUB
        log(f"订阅已更新，节点 IP = {new_ip}")
        return EXIT_OK

    except KeyboardInterrupt:
        log("收到中断信号", "WARN")
        return EXIT_INTERRUPT
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
