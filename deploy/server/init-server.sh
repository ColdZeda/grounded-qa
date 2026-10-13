# ============================================================
# grounded-qa · 服务器初始化脚本（**需用户批准后才执行**）
# ------------------------------------------------------------
# 这个脚本做的是「一次性、幂等」的服务器侧配置。
#
# ⚠️ 权限分级：本脚本属于 🟡 改动类（写系统配置）。
#    按 AGENTS.md §9.2，**必须先公示命令、用户同意后再跑**。
#
# 用法：
#     bash init-server.sh              # 默认 dry-run，只打印将做什么
#     bash init-server.sh --apply      # 真正执行
#     bash init-server.sh --apply --with-slimming --with-sshd-oom
#
# 可选开关（默认都关）：
#     --with-slimming   关停 5 个用不到的系统服务（已在本机执行过，重建服务器时用）
#     --with-sshd-oom   给 sshd 加 OOM 保护（⚠️ 见下方说明）
# ============================================================

set -euo pipefail

APPLY=0
WITH_SLIMMING=0
WITH_SSHD_OOM=0

for arg in "$@"; do
    case "$arg" in
        --apply)          APPLY=1 ;;
        --with-slimming)  WITH_SLIMMING=1 ;;
        --with-sshd-oom)  WITH_SSHD_OOM=1 ;;
        -h|--help)        sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "未知参数：$arg" >&2; exit 2 ;;
    esac
done

APP_DIR=/opt/grounded-qa

# 所有会改系统的动作都走这个函数：dry-run 时只打印
run() {
    if [ "$APPLY" -eq 1 ]; then
        echo "  [执行] $*"
        "$@"
    else
        echo "  [dry-run] $*"
    fi
}

write_file() {
    # write_file <目标路径> <<'EOF' ... EOF
    local target=$1
    local content
    content=$(cat)

    if [ "$APPLY" -eq 1 ]; then
        echo "  [写入] $target"
        if [ -f "$target" ]; then
            cp -a "$target" "${target}.bak.$(date +%Y%m%d%H%M%S)"
        fi
        printf '%s\n' "$content" > "$target"
    else
        echo "  [dry-run 写入] $target"
        printf '%s\n' "$content" | sed 's/^/      | /'
    fi
}

echo "=== grounded-qa 服务器初始化（APPLY=$APPLY）==="

# ------------------------------------------------------------
# 0. 前置检查
# ------------------------------------------------------------
echo
echo "[0/5] 前置检查"
if [ "$(id -u)" -ne 0 ]; then
    echo "  ✗ 需要 root（当前 uid=$(id -u)）" >&2
    exit 1
fi
echo "  ✓ root"
echo "  - 内存：$(free -m | awk '/^Mem:/ {print $7" MB available / "$2" MB total"}')"
echo "  - swap：$(swapon --show --noheadings 2>/dev/null | awk '{print $3}' | paste -sd+ | bc 2>/dev/null || echo '0')"
echo "  - 内核：$(uname -r)"

# ------------------------------------------------------------
# 1. 应用目录
# ------------------------------------------------------------
echo
echo "[1/5] 创建应用目录 $APP_DIR"
run mkdir -p "$APP_DIR"
run chmod 750 "$APP_DIR"

# ------------------------------------------------------------
# 2. Docker registry mirror
#    ⚠️ 大陆直连 Docker Hub / GHCR 拉不动，连 FROM 基础镜像都拉不动。
#       这里**不写死镜像地址**（公共 mirror 经常失效）——
#       请填你自己的阿里云加速器地址。
# ------------------------------------------------------------
echo
echo "[2/5] Docker daemon registry mirror"
echo "  ⚠️ 请把下面的镜像地址换成你自己的阿里云容器镜像加速器地址"
echo "     （阿里云控制台 → 容器镜像服务 → 镜像加速器）"
if [ -f /etc/docker/daemon.json ]; then
    echo "  - 已存在 /etc/docker/daemon.json，**不覆盖**。请自行核对 registry-mirrors 段："
    sed 's/^/      | /' /etc/docker/daemon.json
else
    write_file /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": ["https://<你的ID>.mirror.aliyuncs.com"],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "live-restore": true
}
EOF
    echo "  → 写完需要重启 docker 才生效：systemctl restart docker"
fi

# ------------------------------------------------------------
# 3. 内核参数固化（与系统瘦身配套）
#    ⚠️ 文件名必须是 999- 前缀：/etc/sysctl.d/99-sysctl.conf 是指向
#       /etc/sysctl.conf 的软链，按字典序会覆盖 99-grounded.conf。
#       （AGENTS.md §10 已知坑）
# ------------------------------------------------------------
echo
echo "[3/5] 固化内核参数"
write_file /etc/sysctl.d/999-grounded.conf <<'EOF'
# grounded-qa 固化参数
# ⚠️ 必须是 999- 前缀，才能排在软链 99-sysctl.conf 之后生效。

# 内存紧张时不要过早换出 anon 页；也不要完全不换（swappiness=0 会抬高 OOM 风险）
vm.swappiness = 10
EOF
run sysctl --system

write_file /etc/udev/rules.d/60-grounded-readahead.rules <<'EOF'
# grounded-qa：保留 4 MiB 磁盘预读（原本由 tuned 提供）
# ⚠️ ATTR 的值**必须带引号**，否则 udevadm verify 报
#    "Invalid key/value pair, ignoring"，规则被静默丢弃。（AGENTS.md §10 已知坑）
ACTION=="add|change", KERNEL=="vda", ATTR{queue/read_ahead_kb}="4096"
EOF
run udevadm control --reload
run udevadm verify /etc/udev/rules.d/60-grounded-readahead.rules

# ------------------------------------------------------------
# 4. 系统瘦身（可选）
# ------------------------------------------------------------
if [ "$WITH_SLIMMING" -eq 1 ]; then
    echo
    echo "[4/5] 系统瘦身：关停 5 个用不到的服务"
    echo "  ⚠️ multipathd 是 **socket 激活**的，必须连 .socket 一起关，"
    echo "     否则它随时会被重新拉起。（AGENTS.md §10 已知坑）"
    run systemctl disable --now ModemManager udisks2 multipathd multipathd.socket
    run systemctl disable --now networkd-dispatcher tuned
else
    echo
    echo "[4/5] 系统瘦身：跳过（要执行加 --with-slimming）"
fi

# ------------------------------------------------------------
# 5. sshd OOM 保护（可选，默认关）
#    ⚠️ AGENTS.md §8.2 命令禁区：**不许动 sshd**（SSH 是唯一管理通道）。
#       所以这里只写 drop-in，**不重启 ssh**——它在下次 sshd 重启/开机后才生效。
#       要立即生效需要 systemctl restart ssh，那属于禁区操作，请自行判断。
# ------------------------------------------------------------
if [ "$WITH_SSHD_OOM" -eq 1 ]; then
    echo
    echo "[5/5] sshd OOM 保护（只写配置，不重启）"
    run mkdir -p /etc/systemd/system/ssh.service.d
    write_file /etc/systemd/system/ssh.service.d/oom.conf <<'EOF'
# 内存吃紧时优先杀容器，而不是 sshd —— sshd 一死就失联。
[Service]
OOMScoreAdjust=-900
EOF
    run systemctl daemon-reload
    echo "  ℹ️ 未重启 ssh（禁区）。下次 sshd 重启或机器重启后生效。"
    echo "     当前 sshd 的 oom_score_adj：$(cat /proc/"$(pgrep -o sshd)"/oom_score_adj 2>/dev/null || echo '未知')"
else
    echo
    echo "[5/5] sshd OOM 保护：跳过（要执行加 --with-sshd-oom）"
fi

echo
echo "=== 完成（APPLY=$APPLY）==="
if [ "$APPLY" -eq 0 ]; then
    echo "这次是 dry-run，什么都没改。确认无误后加 --apply 重跑。"
fi
