#!/bin/bash
# fx-gateway 冒烟测试:API、动作、界面(含一级窗口等尺寸断言)
# 生产机上跑可加 FX_SKIP_RESTART=1(跳过内核重启项,避免瞬断);FX_QUOTA_MB 需与桥一致
set -euo pipefail
PORT="${FX_BRIDGE_PORT:-7900}"
QUOTA="${FX_QUOTA_MB:-250}"
SKIP_RESTART="${FX_SKIP_RESTART:-0}"
PASS=0; FAIL=0
ok(){ printf '  ✓ %s\n' "$1"; PASS=$((PASS+1)); }
bad(){ printf '  ✗ %s\n' "$1"; FAIL=$((FAIL+1)); }

echo "== 1. API =="
S=$(curl -sf "http://127.0.0.1:$PORT/api/status")
python3 -c "
import json,sys
d = json.loads('''$S''')
assert d['kernel']['running'], 'kernel not running'
assert d['derived']['level'] in ('green','yellow','red')
assert d['groups']['proxy_now_short'], 'no exit node'
assert len(d['patrol']['events']) >= 1, 'no patrol events'
assert d['patrol']['quota_mb'] == int('''$QUOTA'''), 'quota mismatch: ' + str(d['patrol']['quota_mb'])
" && ok "/api/status 结构完整(配额 $QUOTA)" || bad "/api/status"

L=$(curl -s "http://127.0.0.1:$PORT/api/status" | python3 -c "import json,sys;print(json.load(sys.stdin)['derived']['level'])")
ok "三态推导: $L"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Host: evil.example" "http://127.0.0.1:$PORT/api/status")
[[ "$CODE" == "403" ]] && ok "Host 校验:伪 Host 被拒(403)" || bad "Host 校验(得 $CODE,应为 403)"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/status")
[[ "$CODE" == "200" ]] && ok "Host 校验:本机回环放行" || bad "本机回环被误拒($CODE)"

curl -sf "http://127.0.0.1:$PORT/api/mihomo/version" | grep -q '"version"' && ok "mihomo API 代理" || bad "mihomo API 代理"
curl -sf "http://127.0.0.1:$PORT/" | grep -q 'fx-internet' && ok "UI 页面服务" || bad "UI 页面服务"

echo "== 1.5 速率语义(面板速率 ≈ 全局计数差分实测) =="
S1=$(curl -sf "http://127.0.0.1:$PORT/api/status")
T1=$(curl -sf "http://127.0.0.1:$PORT/api/mihomo/connections" | python3 -c "
import json,sys; d=json.load(sys.stdin); print(d.get('downloadTotal',0)+d.get('uploadTotal',0))")
sleep 4
S2=$(curl -sf "http://127.0.0.1:$PORT/api/status")
T2=$(curl -sf "http://127.0.0.1:$PORT/api/mihomo/connections" | python3 -c "
import json,sys; d=json.load(sys.stdin); print(d.get('downloadTotal',0)+d.get('uploadTotal',0))")
python3 -c "
import json
s1 = json.loads('''$S1''')['traffic']['mbps']; s2 = json.loads('''$S2''')['traffic']['mbps']
true_rate = max(0.0, float($T2) - float($T1)) * 8 / 4.0 / 1e6
shown = (s1 + s2) / 2.0
tol = max(8.0, true_rate * 0.5 + 2.0)
good = abs(shown - true_rate) <= tol
mark = 'OK' if good else 'FAIL'
print('  · 面板 %.1f Mbps vs 实测 %.1f Mbps(容差 %.1f)→ %s' % (shown, true_rate, tol, mark))
raise SystemExit(0 if good else 1)
" && ok "速率语义:面板是真实速率,不是连接累计字节" || bad "速率语义(疑似累计值回归)"

echo "== 2. 动作 =="
curl -sf -X POST "http://127.0.0.1:$PORT/api/actions/handoff-auto" | grep -q '"ok": true' && ok "交还常规模式" || bad "交还常规模式"
if [[ "$SKIP_RESTART" == "1" ]]; then
  ok "内核重启项跳过(FX_SKIP_RESTART=1,生产保护;PROXY 现值不变)"
else
  curl -sf -X POST "http://127.0.0.1:$PORT/api/actions/kernel-restart" | grep -q '"ok": true' && ok "内核重启(KeepAlive 拉起)" || bad "内核重启"
  sleep 3
  curl -sf "http://127.0.0.1:$PORT/api/status" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d['kernel']['running'], 'kernel did not come back after restart'
assert d['groups']['proxy_now'] == 'AUTO', 'handoff did not stick: ' + d['groups']['proxy_now']
" && ok "重启后恢复 + 交还生效(PROXY=AUTO)" || bad "重启后状态"
fi

echo "== 3. 界面(playwright 实测,统一紧凑 400 宽:绿=150 高,黄/红=210 高) =="
cat > /tmp/fx-selftest-ui.cjs << 'EOF'
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 760, height: 900 } });
  await page.goto('http://127.0.0.1:7900/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2600);
  const out = await page.evaluate(async () => {
    const s = await fetch('/api/status').then(r => r.json());
    const win = document.querySelector('.win').getBoundingClientRect();
    const l1 = document.querySelector('.body-l1').getBoundingClientRect();
    return { level: s.derived.level, w: win.width, l1h: l1.height,
             state: document.getElementById('st-text').textContent };
  });
  const checks = [];
  checks.push(['统一紧凑宽 400(当前态 ' + out.level + ')', Math.abs(out.w - 400) < 2]);
  const expH = out.level === 'green' ? 150 : 210;
  checks.push(['[' + out.level + '] 一级内容区高 ' + expH, Math.abs(out.l1h - expH) < 2]);
  checks.push(['状态文案非空: ' + out.state, out.state.length > 1]);
  await page.click('#open-btn');
  await page.waitForTimeout(500);
  const box2 = await page.evaluate(() => document.querySelector('.win').getBoundingClientRect());
  checks.push(['展开详情后宽度仍为 400(仅高度增长)', Math.abs(box2.width - 400) < 2]);
  checks.push(['展开详情后高度增加', box2.height > out.l1h + 100]);
  await page.click('#a-patrol');
  await browser.close();
  console.log(JSON.stringify({ checks }));
})().catch(e => { console.error(JSON.stringify({ fatal: e.message })); process.exit(1); });
EOF
UI=$(NODE_PATH=/opt/homebrew/lib/node_modules node /tmp/fx-selftest-ui.cjs)
if echo "$UI" | python3 -c "
import json,sys
r = json.load(sys.stdin)
if 'fatal' in r: print('  ✗ UI 测试失败:', r['fatal']); sys.exit(1)
for name, passed in r['checks']:
    print(('  ✓ ' if passed else '  ✗ ') + name)
    if not passed: sys.exit(1)
"; then PASS=$((PASS+4)); else FAIL=$((FAIL+1)); fi

echo
echo "结果: $PASS 通过 / $FAIL 失败"
[[ $FAIL -eq 0 ]]
