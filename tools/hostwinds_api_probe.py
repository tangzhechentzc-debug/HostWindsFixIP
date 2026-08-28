#!/usr/bin/env python3
"""探测 Hostwinds Cloud API：验证 key、读取主 IP，并可选测试 fix_isp_blocked 是否被接受。

用法:
  python3 hostwinds_api_probe.py --key-file ~/hostwinds.apikey            # 只读探测（安全）
  python3 hostwinds_api_probe.py --key-file ~/hostwinds.apikey --try-fix  # 额外试探 fix_isp_blocked（可能真的换 IP）
"""
import argparse, json, pathlib, sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_URL = "https://clients.hostwinds.com/cloud/api.php"


def call(action: str, key: str, timeout: float = 30, **extra):
    data = {"action": action, "API": key, **extra}
    body = urlencode(data).encode()
    req = Request(API_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "hostwinds-api-probe/1.0",
    })
    with urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return r.status, json.loads(raw), raw
    except json.JSONDecodeError:
        return r.status, None, raw


def show(label, action, key, **extra):
    print(f"=== {label}  (action={action}) ===")
    try:
        status, parsed, raw = call(action, key, **extra)
    except Exception as e:
        print(f"  [异常] {type(e).__name__}: {e}\n")
        return None
    print(f"  HTTP {status}")
    if parsed is None:
        print(f"  非 JSON 响应（前 300 字）: {raw[:300]!r}")
    else:
        print("  " + json.dumps(parsed, ensure_ascii=False, indent=2)[:1200].replace("\n", "\n  "))
    print()
    return parsed


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--key-file", required=True, type=pathlib.Path)
    p.add_argument("--service-id", required=True, help="Hostwinds 服务 ID")
    p.add_argument("--try-fix", action="store_true",
                   help="试探 fix_isp_blocked；若被接受会真的换 IP（Fix ISP Block 免费）")
    a = p.parse_args()

    key = a.key_file.read_text(encoding="utf-8").strip()
    if not key:
        print("错误：key 文件为空", file=sys.stderr); return 2
    print(f"API key 已载入（{len(key)} 字符，内容不显示）\n")

    # 1) 只读：验证 key 是否有效，并拿到 main_ip
    inst = show("① 验证 key + 读取实例", "get_instance", key, serviceid=a.service_id)
    if isinstance(inst, dict) and isinstance(inst.get("success"), dict):
        s = inst["success"]
        print(f"  >>> main_ip = {s.get('main_ip')}  status = {s.get('status')}\n")

    # 2) 只读：列出实例 IP
    show("② 列出实例 IP", "get_instance_ips", key, serviceid=a.service_id)

    # 3) 关键：文档未记载的 action 是否被接受
    if a.try_fix:
        print("!! 即将试探 fix_isp_blocked —— 若被接受，会真的更换 IP。\n")
        show("③ 试探 fix_isp_blocked", "fix_isp_blocked", key,
             serviceid=a.service_id, loc="ips")
    else:
        print("=== ③ 试探 fix_isp_blocked：已跳过（加 --try-fix 才执行）===")
        print("    先看 ① 是否成功，确认 key 可用后再决定是否试探。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
