import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import hostwinds_autoheal as ah


IPS_BODY = """# 注释行，必须保留
# 另一行注释

198.51.100.20:40000:11111111-2222-3333-4444-555555555555
"""


def results(*, inner: bool, outer: bool) -> dict:
    return {"innerICMP": inner, "innerTCP": inner, "outICMP": outer, "outTCP": outer}


def ipcheck_stdout(ip: str, *, inner: bool, outer: bool) -> str:
    return json.dumps([{"ip": ip, "blocked": not (inner and outer),
                        "results": results(inner=inner, outer=outer), "error": None}])


def completed(rc: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["x"], rc, stdout=stdout, stderr=stderr)


class Sandbox:
    """构造一套临时的 ips.txt / gen-sub.sh / apikey / ipcheck.py。"""

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.dir.name)
        self.ips = root / "ips.txt"; self.ips.write_text(IPS_BODY, encoding="utf-8")
        self.gen = root / "gen-sub.sh"; self.gen.write_text("#!/bin/sh\nexit 0\n"); self.gen.chmod(0o755)
        self.key = root / "k.apikey"; self.key.write_text("SECRET-API-KEY-VALUE\n")
        self.ipcheck = root / "ipcheck.py"; self.ipcheck.write_text("print()\n")
        self.lock = root / "lock"
        self.log = root / "autoheal.log"
        return self

    def __exit__(self, *a):
        self.dir.cleanup()

    def argv(self, *extra):
        return ["--service-id", "1234567", "--ips-file", str(self.ips), "--gen-sub", str(self.gen),
                "--api-key-file", str(self.key), "--ipcheck", str(self.ipcheck),
                "--lock-file", str(self.lock), "--log-file", str(self.log),
                "--settle-wait", "0", "--verify-interval", "1", "--verify-window", "5",
                "--poll-interval", "1", "--ip-wait", "5", "--retry-cooldown", "0",
                *extra]


class IpsFileTests(unittest.TestCase):
    def test_parses_first_valid_line_and_keeps_suffix(self):
        with Sandbox() as sb:
            node = ah.parse_ips_file(sb.ips)
            self.assertEqual(node.ip, "198.51.100.20")
            self.assertEqual(node.suffix, ":40000:11111111-2222-3333-4444-555555555555")

    def test_update_preserves_suffix_comments_and_writes_backup(self):
        with Sandbox() as sb:
            node = ah.parse_ips_file(sb.ips)
            ah.update_ips_file(sb.ips, node, "1.2.3.4")
            text = sb.ips.read_text(encoding="utf-8")
            self.assertIn("1.2.3.4:40000:11111111-2222-3333-4444-555555555555", text)
            self.assertIn("# 注释行，必须保留", text)
            self.assertNotIn("198.51.100.20", text)
            self.assertIn("198.51.100.20", sb.ips.with_suffix(".txt.bak").read_text())

    def test_rejects_file_without_node_line(self):
        with Sandbox() as sb:
            sb.ips.write_text("# 只有注释\n\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                ah.parse_ips_file(sb.ips)


class ClassifyTests(unittest.TestCase):
    def run_check(self, rc, stdout):
        with Sandbox() as sb, mock.patch.object(ah.subprocess, "run",
                                                return_value=completed(rc, stdout)):
            return ah.run_ipcheck(sb.ipcheck, "1.2.3.4", 10)

    def test_exit0_is_clean(self):
        self.assertEqual(self.run_check(0, ipcheck_stdout("1.2.3.4", inner=True, outer=True)).kind, "clean")

    def test_exit1_with_outside_reachable_is_blocked(self):
        self.assertEqual(self.run_check(1, ipcheck_stdout("1.2.3.4", inner=False, outer=True)).kind, "blocked")

    def test_exit1_all_red_is_not_ready(self):
        self.assertEqual(self.run_check(1, ipcheck_stdout("1.2.3.4", inner=False, outer=False)).kind, "not_ready")

    def test_exit1_unparsable_falls_back_to_blocked(self):
        c = self.run_check(1, "not-json")
        self.assertEqual(c.kind, "blocked")
        self.assertIn("保守判为被墙", c.warning)

    def test_exit3_inconclusive_and_exit2_fatal(self):
        self.assertEqual(self.run_check(3, "").kind, "inconclusive")
        self.assertEqual(self.run_check(2, "").kind, "fatal")

    def test_unknown_exit_is_fatal(self):
        self.assertEqual(self.run_check(9, "").kind, "fatal")

    def test_timeout_is_inconclusive(self):
        with Sandbox() as sb, mock.patch.object(
                ah.subprocess, "run", side_effect=subprocess.TimeoutExpired(["x"], 1)):
            self.assertEqual(ah.run_ipcheck(sb.ipcheck, "1.2.3.4", 10).kind, "inconclusive")


class ApiTests(unittest.TestCase):
    def test_submit_fix_success_and_error_and_invalid_action(self):
        cases = [
            ([{"result": "success", "action": "Fix ISP Block",
               "message": "Your IP is being changed!"}], True),
            ([{"result": "error", "action": "Fix ISP Block",
               "message": "A valid serviceid is required."}], False),
            ([{"result": "error", "message": "Invalid Action.", "ERROR": "6734"}], False),
        ]
        for payload, expected in cases:
            with mock.patch.object(ah, "api_call", return_value=payload):
                ok, _ = ah.submit_fix("k", "1", 10)
            self.assertEqual(ok, expected)

    def test_get_instance_redacts_sensitive_fields(self):
        payload = {"success": {"serviceid": 1, "main_ip": "1.2.3.4", "status": "ACTIVE",
                               "password": "ROOT-SECRET", "configkey": "CFG-SECRET"}}
        with mock.patch.object(ah, "api_call", return_value=payload):
            info = ah.get_instance("k", "1", 10)
        self.assertEqual(info["main_ip"], "1.2.3.4")
        self.assertNotIn("password", info)
        self.assertNotIn("configkey", info)
        self.assertNotIn("ROOT-SECRET", json.dumps(info))

    def test_retryable_detection(self):
        self.assertTrue(ah.is_retryable("Please wait before trying again"))
        self.assertTrue(ah.is_retryable("Rate limit exceeded"))
        self.assertFalse(ah.is_retryable("A valid serviceid is required."))


class MainFlowTests(unittest.TestCase):
    def run_main(self, sb, *, ipcheck_seq, extra=(), fix_ok=True, instances=None, gen_rc=0):
        """ipcheck_seq: [(rc, stdout), ...]；instances: main_ip 序列。"""
        checks = list(ipcheck_seq)
        insts = list(instances or [{"main_ip": "198.51.100.20", "status": "ACTIVE"}])

        def fake_run(cmd, **kw):
            if str(sb.gen) in " ".join(map(str, cmd)):
                return completed(gen_rc, "节点数: 1")
            rc, out = checks.pop(0) if checks else (0, ipcheck_stdout("1.2.3.4", inner=True, outer=True))
            return completed(rc, out)

        def fake_instance(key, sid, timeout):
            return insts[0] if len(insts) == 1 else insts.pop(0)

        out = io.StringIO()
        with (
            mock.patch.object(ah.subprocess, "run", side_effect=fake_run),
            mock.patch.object(ah, "get_instance", side_effect=fake_instance),
            mock.patch.object(ah, "submit_fix", return_value=(fix_ok, "Your IP is being changed!")),
            mock.patch.object(ah.time, "sleep", lambda *_: None),
            redirect_stdout(out),
        ):
            code = ah.main(sb.argv(*extra))
        return code, out.getvalue()

    def test_clean_ip_does_nothing(self):
        with Sandbox() as sb:
            before = sb.ips.read_text()
            code, out = self.run_main(sb, ipcheck_seq=[(0, ipcheck_stdout("198.51.100.20", inner=True, outer=True))])
            self.assertEqual(code, ah.EXIT_OK)
            self.assertIn("未被封锁", out)
            self.assertEqual(sb.ips.read_text(), before)

    def test_all_red_returns_vps_down_without_changing_ip(self):
        with Sandbox() as sb:
            before = sb.ips.read_text()
            code, out = self.run_main(sb, ipcheck_seq=[(1, ipcheck_stdout("198.51.100.20", inner=False, outer=False))])
            self.assertEqual(code, ah.EXIT_VPS_DOWN)
            self.assertIn("疑似宕机", out)
            self.assertEqual(sb.ips.read_text(), before)

    def test_ipcheck_error_exit3_is_quiet_noop(self):
        with Sandbox() as sb:
            code, out = self.run_main(sb, ipcheck_seq=[(3, "")])
            self.assertEqual(code, ah.EXIT_OK)
            self.assertIn("无结论", out)

    def test_dry_run_never_changes_anything(self):
        with Sandbox() as sb:
            before = sb.ips.read_text()
            code, out = self.run_main(
                sb, extra=("--dry-run",),
                ipcheck_seq=[(1, ipcheck_stdout("198.51.100.20", inner=False, outer=True))])
            self.assertEqual(code, ah.EXIT_OK)
            self.assertIn("[dry-run]", out)
            self.assertEqual(sb.ips.read_text(), before)

    def test_blocked_ip_runs_full_chain(self):
        with Sandbox() as sb:
            code, out = self.run_main(
                sb,
                instances=[{"main_ip": "198.51.100.20"}, {"main_ip": "198.51.100.20"},
                           {"main_ip": "9.9.9.9"}, {"main_ip": "9.9.9.9"}],
                ipcheck_seq=[
                    (1, ipcheck_stdout("198.51.100.20", inner=False, outer=True)),  # 巡检：被墙
                    (0, ipcheck_stdout("9.9.9.9", inner=True, outer=True)),           # 验证：干净
                ])
            self.assertEqual(code, ah.EXIT_OK)
            self.assertIn("9.9.9.9", sb.ips.read_text())
            self.assertIn("订阅已更新", out)
            self.assertIn(":40000:11111111-2222-3333-4444-555555555555", sb.ips.read_text())

    def test_gen_sub_failure_keeps_updated_ips_and_returns_6(self):
        with Sandbox() as sb:
            code, out = self.run_main(
                sb, gen_rc=1,
                instances=[{"main_ip": "198.51.100.20"}, {"main_ip": "198.51.100.20"},
                           {"main_ip": "9.9.9.9"}, {"main_ip": "9.9.9.9"}],
                ipcheck_seq=[
                    (1, ipcheck_stdout("198.51.100.20", inner=False, outer=True)),
                    (0, ipcheck_stdout("9.9.9.9", inner=True, outer=True)),
                ])
            self.assertEqual(code, ah.EXIT_GENSUB)
            self.assertIn("9.9.9.9", sb.ips.read_text())  # 保留已更新，不回滚

    def test_exhausted_when_every_new_ip_still_blocked(self):
        with Sandbox() as sb:
            before = sb.ips.read_text()
            seq = [(1, ipcheck_stdout("198.51.100.20", inner=False, outer=True))]
            seq += [(1, ipcheck_stdout("9.9.9.9", inner=False, outer=True))] * 8
            code, out = self.run_main(
                sb, extra=("--max-cycles", "1", "--verify-confirm", "2"),
                instances=[{"main_ip": "198.51.100.20"}, {"main_ip": "198.51.100.20"},
                           {"main_ip": "9.9.9.9"}, {"main_ip": "9.9.9.9"}],
                ipcheck_seq=seq)
            self.assertEqual(code, ah.EXIT_EXHAUSTED)
            self.assertEqual(sb.ips.read_text(), before)

    def test_cooldown_no_ip_change_stops_run_without_hammering(self):
        """提交受理但 IP 始终不变（冷却）→ 本次运行停止，退出码 3，且只提交过一次。"""
        with Sandbox() as sb:
            before = sb.ips.read_text()
            submit_calls = []
            def counting_submit(key, sid, timeout):
                submit_calls.append(1)
                return (True, "Your IP is being changed!")
            # get_instance 始终返回同一个 IP（模拟冷却：换不动）
            def fake_instance(key, sid, timeout):
                return {"main_ip": "198.51.100.20", "status": "ACTIVE"}
            def fake_run(cmd, **kw):
                if str(sb.gen) in " ".join(map(str, cmd)):
                    return completed(0, "节点数: 1")
                return completed(1, ipcheck_stdout("198.51.100.20", inner=False, outer=True))
            out = io.StringIO()
            with (
                mock.patch.object(ah.subprocess, "run", side_effect=fake_run),
                mock.patch.object(ah, "get_instance", side_effect=fake_instance),
                mock.patch.object(ah, "submit_fix", side_effect=counting_submit),
                mock.patch.object(ah.time, "sleep", lambda *_: None),
                redirect_stdout(out),
            ):
                code = ah.main(sb.argv("--max-cycles", "5"))
            self.assertEqual(code, ah.EXIT_EXHAUSTED)
            self.assertIn("疑似换 IP 冷却", out.getvalue())
            self.assertEqual(len(submit_calls), 1)   # 只刷一次，没有连刷
            self.assertEqual(sb.ips.read_text(), before)

    def test_lock_held_exits_quietly(self):
        with Sandbox() as sb:
            holder = ah.acquire_lock(sb.lock)
            self.assertIsNotNone(holder)
            out = io.StringIO()
            with redirect_stdout(out):
                code = ah.main(sb.argv())
            holder.close()
            self.assertEqual(code, ah.EXIT_OK)
            self.assertIn("锁被占用", out.getvalue())

    def test_secrets_never_appear_in_output_or_log(self):
        with Sandbox() as sb:
            _, out = self.run_main(
                sb,
                instances=[{"main_ip": "198.51.100.20", "status": "ACTIVE"}],
                ipcheck_seq=[(0, ipcheck_stdout("198.51.100.20", inner=True, outer=True))])
            self.assertNotIn("SECRET-API-KEY-VALUE", out)
            self.assertNotIn("SECRET-API-KEY-VALUE", sb.log.read_text(encoding="utf-8"))



class RetryTests(unittest.TestCase):
    def test_get_instance_retry_recovers_after_transient_error(self):
        calls = []
        def flaky(key, sid, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise ah.ApiError("TimeoutError")
            return {"main_ip": "1.2.3.4"}
        with (
            mock.patch.object(ah, "get_instance", side_effect=flaky),
            mock.patch.object(ah.time, "sleep", lambda *_: None),
        ):
            info = ah.get_instance_retry("k", "1", 10, attempts=3, cooldown=0)
        self.assertEqual(info["main_ip"], "1.2.3.4")
        self.assertEqual(len(calls), 3)

    def test_get_instance_retry_raises_after_exhausting_attempts(self):
        with (
            mock.patch.object(ah, "get_instance", side_effect=ah.ApiError("TimeoutError")),
            mock.patch.object(ah.time, "sleep", lambda *_: None),
        ):
            with self.assertRaises(ah.ApiError):
                ah.get_instance_retry("k", "1", 10, attempts=3, cooldown=0)

    def test_transient_timeout_midloop_does_not_abort_whole_heal(self):
        """第 1 轮换到被封 IP；第 2 轮开头读 IP 超时一次后恢复，最终换到干净 IP。"""
        with Sandbox() as sb:
            seq = [
                {"main_ip": "198.51.100.20", "status": "ACTIVE"},
                {"main_ip": "198.51.100.20"},
                {"main_ip": "198.51.100.20"}, {"main_ip": "9.9.9.9"},
                ah.ApiError("TimeoutError"),
                {"main_ip": "9.9.9.9"},
                {"main_ip": "9.9.9.9"}, {"main_ip": "8.8.8.8"},
            ]
            def fake_instance(key, sid, timeout):
                item = seq.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            checks = [
                (1, ipcheck_stdout("198.51.100.20", inner=False, outer=True)),
                (1, ipcheck_stdout("9.9.9.9", inner=False, outer=True)),
                (1, ipcheck_stdout("9.9.9.9", inner=False, outer=True)),
                (0, ipcheck_stdout("8.8.8.8", inner=True, outer=True)),
            ]
            def fake_run(cmd, **kw):
                if str(sb.gen) in " ".join(map(str, cmd)):
                    return completed(0, "节点数: 1")
                rc, out = checks.pop(0) if checks else (0, ipcheck_stdout("8.8.8.8", inner=True, outer=True))
                return completed(rc, out)
            out = io.StringIO()
            with (
                mock.patch.object(ah.subprocess, "run", side_effect=fake_run),
                mock.patch.object(ah, "get_instance", side_effect=fake_instance),
                mock.patch.object(ah, "submit_fix", return_value=(True, "Your IP is being changed!")),
                mock.patch.object(ah.time, "sleep", lambda *_: None),
                redirect_stdout(out),
            ):
                code = ah.main(sb.argv("--verify-confirm", "2", "--submit-retries", "3"))
            self.assertEqual(code, ah.EXIT_OK)
            self.assertIn("8.8.8.8", sb.ips.read_text())
            self.assertIn("读取实例失败", out.getvalue())


if __name__ == "__main__":
    unittest.main()
