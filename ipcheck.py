#!/usr/bin/env python3
"""
ipcheck —— 封装 vps234.com 的 IP 封锁检测接口

检测指定 IP 在「国内 / 国外」的 ICMP 与 TCP 可达情况。
四项全部为绿（True）才代表该 IP 未被封锁。

用法:
    python3 ipcheck.py 1.2.3.4                # 检测单个 IP
    python3 ipcheck.py 1.2.3.4 5.6.7.8 ...    # 检测多个 IP
    python3 ipcheck.py --json 1.2.3.4         # 输出 JSON
    echo -e "1.2.3.4\\n5.6.7.8" | python3 ipcheck.py -   # 从标准输入读取

退出码:
    0  所有被检测的 IP 均未被封锁（四项全绿）
    1  至少有一个 IP 被封锁
    2  用法错误 / 输入非法
    3  网络或接口错误
"""

import argparse
import ipaddress
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://www.vps234.com/ipcheck/getdata/"
FIELDS = ("innerICMP", "innerTCP", "outICMP", "outTCP")
FIELD_LABELS = {
    "innerICMP": "国内 ICMP",
    "innerTCP": "国内 TCP",
    "outICMP": "国外 ICMP",
    "outTCP": "国外 TCP",
}

GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


def is_valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def query_ip(ip: str, timeout: float = 15.0, retries: int = 2):
    """调用接口，返回四项布尔值 dict。失败时抛出 RuntimeError。"""
    payload = urllib.parse.urlencode(
        {"idName": "item%d" % int(time.time() * 1000), "ip": ip}
    ).encode()
    headers = {
        "User-Agent": "Mozilla/5.0 (ipcheck-script)",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.vps234.com/ipchecker/",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(API_URL, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
            else:
                raise RuntimeError("请求失败: %s" % e)

    if body.get("error"):
        raise RuntimeError("接口返回错误: %s" % body)
    inner = body.get("data", {})
    if not inner.get("success"):
        raise RuntimeError("接口检测失败: %s" % inner.get("msg", body))
    data = inner.get("data", {})
    return {f: bool(data.get(f, False)) for f in FIELDS}


def check(ip: str, **kw):
    """返回 (blocked: bool, results: dict|None, error: str|None)."""
    try:
        results = query_ip(ip, **kw)
    except RuntimeError as e:
        return None, None, str(e)
    blocked = not all(results.values())
    return blocked, results, None


def format_human(ip, blocked, results, error):
    if error:
        return "%s%-15s%s  %s错误%s: %s" % (BOLD, ip, RESET, RED, RESET, error)
    marks = []
    for f in FIELDS:
        ok = results[f]
        sym = "%s✓%s" % (GREEN, RESET) if ok else "%s✗%s" % (RED, RESET)
        marks.append("%s %s" % (FIELD_LABELS[f], sym))
    status = (
        "%s未封锁%s" % (GREEN, RESET)
        if not blocked
        else "%s已封锁%s" % (RED, RESET)
    )
    return "%s%-15s%s  [%s]  %s" % (BOLD, ip, RESET, status, "  ".join(marks))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="检测 IP 是否被封锁（四项全绿 = 未封锁）"
    )
    parser.add_argument(
        "ips",
        nargs="+",
        help="要检测的 IP 地址；用 - 表示从标准输入按行读取",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="单次请求超时秒数（默认 15）"
    )
    args = parser.parse_args(argv)

    # 展开 IP 列表（支持从 stdin）
    ips = []
    for item in args.ips:
        if item == "-":
            ips.extend(line.strip() for line in sys.stdin if line.strip())
        else:
            ips.append(item)

    if not ips:
        print("错误: 未提供任何 IP", file=sys.stderr)
        return 2

    invalid = [ip for ip in ips if not is_valid_ip(ip)]
    if invalid:
        print("错误: 非法 IP: %s" % ", ".join(invalid), file=sys.stderr)
        return 2

    any_blocked = False
    any_error = False
    json_out = []

    for ip in ips:
        blocked, results, error = check(ip, timeout=args.timeout)
        if error:
            any_error = True
        elif blocked:
            any_blocked = True

        if args.json:
            json_out.append(
                {
                    "ip": ip,
                    "blocked": blocked,
                    "results": results,
                    "error": error,
                }
            )
        else:
            print(format_human(ip, blocked, results, error))

    if args.json:
        print(json.dumps(json_out, ensure_ascii=False, indent=2))

    if any_error:
        return 3
    if any_blocked:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
