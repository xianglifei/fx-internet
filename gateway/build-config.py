#!/usr/bin/env python3
"""fx-gateway 配置生成器:profile 模板 → mihomo runtime.yaml

- 订阅 URL 优先级:--sub-url 参数 > state/subscription.txt(持久化,脱离 Verge 后仍可用)
  > 从现有 Verge 部署的 profiles/jms.yaml 自动提取(首次迁移用)
- 托管字段(authoritative)在生成块中钉死,模板不涉及这些键,不会重复
- 生成后用 mihomo -t 校验,通过才原子替换;内容不变则跳过
"""
import argparse
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(REPO, "clash", "profile.yaml.template")
VERGE_PROFILE = "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles/jms.yaml"


def extract_sub_from_verge():
    try:
        text = os.path.expanduser(VERGE_PROFILE)
        with open(text) as f:
            m = re.search(r'url:\s*"(https?://[^"]+)"', f.read())
        return m.group(1) if m else None
    except OSError:
        return None


def resolve_sub_url(args, state_dir):
    sub_file = os.path.join(state_dir, "subscription.txt")
    if args.sub_url:
        with open(sub_file, "w") as f:
            f.write(args.sub_url + "\n")
        os.chmod(sub_file, 0o600)
        return args.sub_url
    if os.path.exists(sub_file):
        with open(sub_file) as f:
            url = f.read().strip()
        if url:
            return url
    url = extract_sub_from_verge()
    if not url:
        sys.exit("错误:找不到订阅 URL。用 --sub-url 指定,或先部署过 Verge 供自动提取。")
    os.makedirs(state_dir, exist_ok=True)
    with open(sub_file, "w") as f:
        f.write(url + "\n")
    os.chmod(sub_file, 0o600)
    print(f"已从 Verge 提取订阅 URL 并持久化到 {sub_file}")
    return url


def build(sub_url, mixed_port, socket_path):
    with open(TEMPLATE) as f:
        body = f.read().replace("__SUB_URL__", sub_url)
    managed = "\n".join([
        "# ==== fx-gateway 托管字段(生成器维护,勿手改)====",
        f"mixed-port: {mixed_port}",
        "allow-lan: false",
        "ipv6: true",
        "external-controller: ''",
        f'external-controller-unix: "{socket_path}"',
        "profile:",
        "  store-selected: true",
        "",
    ])
    return managed + "\n" + body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7898,
                    help="混合端口(试验 7898 / 生产 7897)")
    ap.add_argument("--state-dir", default=os.path.expanduser(
        "~/Library/Application Support/fx-internet"))
    ap.add_argument("--sub-url", default=None,
                    help="订阅 URL(不给则用已持久化的或从 Verge 提取)")
    ap.add_argument("--mihomo", default=os.path.expanduser("~/.local/bin/mihomo"))
    args = ap.parse_args()

    state_dir = args.state_dir
    os.makedirs(state_dir, exist_ok=True)
    socket_path = os.path.join(state_dir, "fx-mihomo.sock")
    if len(socket_path.encode()) >= 104:
        sys.exit(f"错误:socket 路径超过 103 字节(sun_path 限制):{socket_path}")

    sub_url = resolve_sub_url(args, state_dir)
    content = build(sub_url, args.port, socket_path)
    runtime = os.path.join(state_dir, "runtime.yaml")

    if os.path.exists(runtime):
        with open(runtime) as f:
            if f.read() == content:
                if (os.stat(runtime).st_mode & 0o777) != 0o600:
                    os.chmod(runtime, 0o600)
                    print("runtime.yaml 权限已收紧为 0600(内含订阅 URL)")
                print(f"runtime.yaml 内容无变化,跳过(端口 {args.port})")
                return

    tmp = runtime + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.chmod(tmp, 0o600)  # 订阅 URL = 节点密码,与 subscription.txt 同级保护

    r = subprocess.run([args.mihomo, "-t", "-d", state_dir, "-f", tmp],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        os.remove(tmp)
        sys.exit(f"错误:mihomo -t 校验失败,不落盘\n{r.stdout}\n{r.stderr}")
    os.replace(tmp, runtime)
    print(f"runtime.yaml 已生成并校验通过 → {runtime}(mixed-port {args.port})")


if __name__ == "__main__":
    main()
