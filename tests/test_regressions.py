"""Offline regression tests: no launchd, networksetup, or live network calls."""
import datetime
import email.message
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


b = module('bridge', 'gateway/fx-bridge.py')
p = module('patrol', 'patrol/verge-node-patrol.py')


class Regressions(unittest.TestCase):
    def test_live_exit_rolls_back_once(self):
        source = (ROOT / 'gateway/cutover.sh').read_text()
        definitions = source[source.index('die(){'):source.index('\nrollback(){')]
        for stage, command, expected in [('live', 'false || die test', 1),
                                          ('live', 'false', 1),
                                          ('pre', 'false || die test', 0),
                                          ('done', 'true', 0)]:
            with self.subTest(stage=stage, command=command):
                result = subprocess.run(['bash', '-c', 'set -euo pipefail\nSTAGE=' + stage +
                    '\nrollback(){ echo ROLLBACK; }\n' + definitions + '\n' + command],
                    capture_output=True, text=True)
                self.assertEqual(result.stdout.count('ROLLBACK'), expected)

    def test_write_auth(self):
        h = object.__new__(b.Handler)
        h.headers = email.message.Message()
        h.headers['Host'] = f'127.0.0.1:{b.BRIDGE_PORT}'
        self.assertFalse(h._write_ok())
        h.headers['X-FX-Token'] = b.WRITE_TOKEN
        self.assertTrue(h._write_ok())  # authenticated CLI
        h.headers['Origin'] = 'https://evil.example'
        self.assertFalse(h._write_ok())
        h.headers.replace_header('Origin', f'http://127.0.0.1:{b.BRIDGE_PORT}')
        self.assertTrue(h._write_ok())
        h.headers.replace_header('Host', 'evil.example')
        self.assertFalse(h._write_ok())

    def test_api_rejects_transport_http_and_invalid_json(self):
        for m, call in [(b, lambda: b.mi('GET', '/version')),
                         (p, lambda: p.api('/mock', 'GET', '/version'))]:
            for code, stdout in [(7, '\n000'), (0, '{"error":"bad"}\n500'),
                                 (0, 'invalid\n200')]:
                with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, stdout, '')):
                    with self.assertRaises(RuntimeError):
                        call()
            with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '\n204', '')):
                self.assertEqual(call(), {})

    def test_handoff_failure_not_success(self):
        with tempfile.TemporaryDirectory() as d, patch.object(b, 'STATE_DIR', d), patch.object(b, 'PATROL_LOCK', d + '/lock'), patch.object(b, 'mi', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                b.handoff_auto()

    def test_switch_requires_readback(self):
        with patch.object(p, 'api', return_value={'now': 'old'}):
            with self.assertRaises(RuntimeError):
                p.select('/mock', 'new')

    def test_sweep_restores_on_success_failure_and_all_dead(self):
        for measures in [[(True, 20), (True, 30)], [(False, 0), (False, 0)],
                         [(True, 20), RuntimeError('speed endpoint')]]:
            selected = []
            with patch.object(p, 'api', return_value={'now': 'AUTO'}), patch.object(p, 'select', side_effect=lambda sock, name: selected.append(name)), patch.object(p, 'measure', side_effect=measures), patch.object(p, 'log'):
                try:
                    p.sweep('/mock', ['A', 'B'])
                except RuntimeError:
                    pass
                self.assertEqual(selected[-1], 'AUTO')

    def test_measure_reports_speed_service_failure(self):
        # ac07fb4 起:测速失败返回 (False, 0.0) 而非抛异常——抛异常会被外层兜底
        # 吞掉并以"保留原出口"收场,应急换节点分支走不到(2026-11-08 死节点悬置
        # 4 小时的根因)。
        with patch.object(p, 'alive', return_value=True), patch.object(p, 'check_budget'), patch.object(p, 'curl_measure', return_value=('503', 10000000)):
            self.assertEqual(p.measure(4000000), (False, 0.0))

    def test_partial_and_failed_download_are_charged_but_rejected(self):
        for code, output in [(0, '503 10000 200'), (28, '200 10000 200'), (0, '200 10000 200')]:
            with patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, output, '')), patch.object(p, 'charge') as charge:
                self.assertEqual(p.curl_measure('https://mock', 1, 4000000), ('000', 0.0))
                charge.assert_called_once_with(200)

    def test_budget_reserve_and_persistent_usage(self):
        with tempfile.TemporaryDirectory() as d, patch.object(p, 'USAGE_PATH', d+'/usage'), patch.object(p, 'QUOTA_MB', 4), patch.object(p, 'EMERGENCY_MB', 8), patch.object(p, 'EMERGENCY', False):
            p.charge(4000000)
            with self.assertRaises(p.BudgetExceeded):
                p.check_budget(1)
            with patch.object(p, 'EMERGENCY', True):
                p.check_budget(8000000)
                with self.assertRaises(p.BudgetExceeded):
                    p.check_budget(8000001)
            self.assertEqual(p.usage()['bytes'], 4000000)

    def test_flock(self):
        with tempfile.TemporaryDirectory() as d, patch.object(p, 'PATROL_LOCK', d+'/lock'):
            first = p.acquire_lock()
            try:
                self.assertIsNone(p.acquire_lock())
            finally:
                first.close()
            again = p.acquire_lock()
            self.assertIsNotNone(again)
            again.close()

    def test_https_socks_checked(self):
        cur = {key + suffix: value for key in ('HTTP','HTTPS','SOCKS')
               for suffix, value in [('Enable','1'),('Proxy','127.0.0.1'),('Port',str(b.MIXED_PORT))]}
        self.assertTrue(b.proxy_matches(cur))
        cur['HTTPSEnable'] = '0'
        self.assertFalse(b.proxy_matches(cur))

    def test_cleanup_only_owned_items(self):
        writes = []
        def run(cmd, **kwargs):
            if cmd[1].startswith('-get'):
                server = '127.0.0.1' if cmd[1] == '-getsecurewebproxy' else 'company.proxy'
                return subprocess.CompletedProcess(cmd, 0, f'Enabled: Yes\nServer: {server}\nPort: {b.MIXED_PORT}\n', '')
            writes.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, '', '')
        with patch.object(b, 'network_services', return_value=['Wi-Fi']), patch.object(b.subprocess, 'run', side_effect=run):
            b.clear_owned_proxies()
        self.assertEqual(writes, [['networksetup', '-setsecurewebproxystate', 'Wi-Fi', 'off']])

    def test_cleanup_failure_prevents_stop(self):
        with patch.object(b, 'SYSAPPLY', True), patch.object(b, 'clear_owned_proxies', side_effect=RuntimeError('denied')), patch.object(b, 'launchctl') as launch:
            with self.assertRaises(RuntimeError):
                b.do_kernel_stop()
            launch.assert_not_called()

    def test_health_and_usage_not_limited_by_event_tail(self):
        now = datetime.datetime.now().isoformat()
        event = {'type': 'steady_ok', 'ts': now}
        mon = {'kernel':'mock', 'ts':__import__('time').time(), 'proxies':{}}
        with tempfile.TemporaryDirectory() as d, patch.object(b, 'EVENTS_PATH', d+'/events'), patch.object(b, 'MON', mon), patch.object(b, 'read_events', return_value=[event]), patch.object(b, 'SYSAPPLY', False):
            with open(d+'/events.usage.json', 'w') as f:
                json.dump({'date': datetime.date.today().isoformat(), 'bytes': 123456789}, f)
            self.assertEqual(b.build_status()['derived']['reason'], 'no_nodes')
            self.assertEqual(b.build_status()['patrol']['cost_today_mb'], 123.46)
            mon['proxies'] = {'PROXY': {'now':'AUTO', 'all':['AUTO','JMS']}}
            mon['ts'] = 1
            self.assertEqual(b.build_status()['derived']['reason'], 'status_unknown')

    def test_stopped_kernel_never_reenables_proxy_from_stale_monitor(self):
        with patch.object(b, 'SYSAPPLY', True), patch.object(b, 'KERNEL_WANTED', False), patch.object(b, 'MON', {'kernel':'mock', 'ts':__import__('time').time()}), patch.object(b, 'read_sysproxy', return_value={}), patch.object(b, 'clear_owned_proxies') as clear, patch.object(b, 'apply_proxy_to_service') as apply:
            b.guard_once()
            clear.assert_called_once()
            apply.assert_not_called()

    def test_guard_repairs_missing_https(self):
        full = {k+s:v for k in ('HTTP','HTTPS','SOCKS') for s,v in
                [('Enable','1'), ('Proxy','127.0.0.1'), ('Port',str(b.MIXED_PORT))]}
        partial = dict(full, HTTPSEnable='0')
        with patch.object(b, 'SYSAPPLY', True), patch.object(b, 'KERNEL_WANTED', True), patch.object(b, 'MON', {'kernel':'mock', 'ts':__import__('time').time()}), patch.object(b, 'read_sysproxy', side_effect=[partial, full]), patch.object(b, 'primary_service', return_value='Ethernet'), patch.object(b, 'apply_proxy_to_service', return_value=None) as apply:
            b.guard_once()
            apply.assert_called_once_with('Ethernet', enable=True)
            self.assertTrue(b.SYS['applied'])

    def test_budget_interrupt_restores_sweep(self):
        selections = []
        with patch.object(p, 'api', return_value={'now':'original'}), patch.object(p, 'select', side_effect=lambda sock,n: selections.append(n)), patch.object(p, 'measure', side_effect=p.BudgetExceeded('limit')):
            with self.assertRaises(p.BudgetExceeded):
                p.sweep('/mock', ['A','B'])
        self.assertEqual(selections, ['A', 'original'])

    def test_default_smoke_has_no_ui_mutations(self):
        source = (ROOT/'gateway/selftest.sh').read_text()
        self.assertIn('ALLOW_MUTATIONS="${FX_ALLOW_MUTATIONS:-0}"', source)
        self.assertNotIn("page.click('#a-patrol')", source)

    def test_direct_probe_204_and_failures(self):
        class Resp204:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class Opener204:
            def open(self, req, timeout=None): return Resp204()
        class OpenerFail:
            def open(self, req, timeout=None): raise b.urllib.error.URLError('timeout')
        with patch.object(b.urllib.request, 'build_opener', return_value=Opener204()):
            self.assertTrue(b.direct_check_once())
            self.assertTrue(b.DIRECT['ok'])
            self.assertGreater(b.DIRECT['ts'], 0)
        with patch.object(b.urllib.request, 'build_opener', return_value=OpenerFail()):
            self.assertFalse(b.direct_check_once())
            self.assertFalse(b.DIRECT['ok'])

    def test_status_payload_carries_direct_and_ui_keeps_contract(self):
        payload = b.build_status()
        self.assertIn('direct', payload)
        self.assertIn('ok', payload['direct'])
        ui = (ROOT/'gateway/ui/index.html').read_text()
        # 方案B去重版:状态带只留双 chip(出口唯一答案在表格「使用中」标记,直连卡已删)
        for needle in ('chip-proxy', 'chip-direct', 'id="st-text"', 'width:max-content',
                       'min-width:460px', 'max-width:720px'):
            self.assertIn(needle, ui)
        self.assertNotIn('direct-card', ui)
        self.assertNotIn('strip-exit', ui)
        # 机制细节收进测速历史子页:入口按钮 + 额度大白话文案 + 倒序历史
        for needle in ('a-history', 'h-quota', '只影响测速排名,联网不受影响', '/api/events'):
            self.assertIn(needle, ui)
        # 操作按钮按需出现:交还改名「恢复自动选优」(默认 hidden,钉住时显示);重启内核默认 hidden
        self.assertIn('id="a-handoff" hidden>恢复自动选优', ui)
        self.assertIn('id="a-restart" hidden>重启内核', ui)
        # 菜单栏 App 页面自更新契约(版本占位符由桥注入)
        self.assertIn('__UI_VERSION__', ui)

    def test_quiet_and_sweep_hours(self):
        # 夜间 0-7 点只查连通不测吞吐;白天 8/11/14/17/20/23 点定时全量实测
        for h in range(0, 8):
            self.assertTrue(p.is_quiet_hour(datetime.datetime(2026, 11, 18, h, 10)))
            self.assertFalse(p.is_sweep_hour(datetime.datetime(2026, 11, 18, h, 10)))
        for h in (8, 11, 14, 17, 20, 23):
            self.assertFalse(p.is_quiet_hour(datetime.datetime(2026, 11, 17, h, 10)))
            self.assertTrue(p.is_sweep_hour(datetime.datetime(2026, 11, 17, h, 10)))
        for h in (9, 10, 12, 13, 15, 16, 18, 19, 21, 22):
            self.assertFalse(p.is_quiet_hour(datetime.datetime(2026, 11, 17, h, 10)))
            self.assertFalse(p.is_sweep_hour(datetime.datetime(2026, 11, 17, h, 10)))


if __name__ == '__main__':
    unittest.main()
