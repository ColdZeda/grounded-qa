#!/usr/bin/env bash
# ============================================================
# 生产库备份 —— pg_dump（自定义格式，可选择性恢复）＋ 轮转
# ------------------------------------------------------------
# 设计要点（每一条都对应一次"看起来成功、其实没用"的失败模式）：
#
# ① **先写 .part，校验通过再改名**
#    否则一次网络中断/磁盘满会留下一个"看起来像备份"的半截文件，
#    而这个目录将来是要被还原脚本按时间取最新一份的。
#
# ② **校验的是"能不能读"，不是"文件在不在"**
#    非空 + `pg_restore -l` 能列出目录，才叫可用。0 字节的 dump 也能 `ls` 到。
#
# ③ **轮转按文件个数，不按天数**
#    "保留 7 天"在"连续 7 天备份都失败"的情况下等于**一份都没有**；
#    按份数保留，至少保证"最近 N 次成功的备份都还在"。
#
# ④ 输出一行**机器可读**的结论（systemd/cron 日志里就靠这一行判断）
#
# ⚠️ 这份备份**和数据库在同一块盘上** —— 防的是"误删/迁移出错/库被写坏"，
#    **不防磁盘故障**。异地/对象存储不在 v1.1 范围（见 architecture.md §9.32 的代价表）。
# ============================================================
set -euo pipefail

CONTAINER="${CONTAINER:-gq-postgres}"
BACKUP_DIR="${BACKUP_DIR:-/opt/grounded-qa/backups}"
KEEP="${KEEP:-7}"
#: 小于这个字节数的 dump 一律当失败（空库的 dump 也有几十 KB；10000 是个地板）
MIN_BYTES="${MIN_BYTES:-10000}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_DIR/gq-$STAMP.dump"
TMP="$TARGET.part"

mkdir -p "$BACKUP_DIR"
trap 'rm -f "$TMP"' EXIT

# 用户/库名从**容器内的环境变量**取，不在宿主机上另存一份
docker exec "$CONTAINER" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$TMP"

SIZE="$(stat -c %s "$TMP")"
if [ "$SIZE" -lt "$MIN_BYTES" ]; then
  echo "FAIL dump-too-small size=$SIZE target=$TARGET" >&2
  exit 1
fi

# 能列出目录 = 这个 dump 读得动（比"文件存在"强得多）
if ! docker exec -i "$CONTAINER" pg_restore -l < "$TMP" > /dev/null; then
  echo "FAIL unreadable-dump target=$TARGET" >&2
  exit 1
fi

mv "$TMP" "$TARGET"

# 轮转：按修改时间倒序，留前 KEEP 份，其余删掉
if [ "$KEEP" -gt 0 ]; then
  ls -1t "$BACKUP_DIR"/gq-*.dump 2>/dev/null \
    | tail -n +"$((KEEP + 1))" \
    | while read -r old; do
        rm -f "$old"
      done
fi

printf '%s OK %s %s\n' "$(date -Is)" "$TARGET" "$(numfmt --to=iec "$SIZE")"
