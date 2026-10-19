#!/usr/bin/env bash
# ============================================================
# 恢复演练 —— 把**最新一份**备份恢复到一个**临时库**，逐表比对行数，然后删掉临时库
# ------------------------------------------------------------
# ⚠️ 这个脚本**只碰自己创建的 `gq_drill_<时间戳>` 库**，绝不写生产库。
#    库名带时间戳：连跑两次也不会撞车，也不会误删别人的库。
#
# ⚠️ **为什么必须真恢复一次**：备份"能生成"和"能恢复"是两件事 ——
#    dump 可能因为权限、扩展（pgvector）、编码而恢复失败，而这类问题
#    **只有恢复过一次才会暴露**。没验过的备份等于没有备份。
#
# 用法：./restore-drill.sh            # 用最新的那份
#       ./restore-drill.sh <dump>     # 指定某一份
# ============================================================
set -euo pipefail

CONTAINER="${CONTAINER:-gq-postgres}"
BACKUP_DIR="${BACKUP_DIR:-/opt/grounded-qa/backups}"
#: 要比对的表（顺序就是输出顺序）
TABLES="${TABLES:-documents chunks users conversations messages}"
DRILL_DB="gq_drill_$(date -u +%Y%m%d_%H%M%S)"

DUMP="${1:-}"
if [ -z "$DUMP" ]; then
  DUMP="$(ls -1t "$BACKUP_DIR"/gq-*.dump 2>/dev/null | head -1 || true)"
fi
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
  echo "FAIL no-backup-found dir=$BACKUP_DIR" >&2
  exit 1
fi

# 在指定库里跑一条 SQL，返回去空白的结果
psql_db() { # $1=db  $2=sql
  docker exec "$CONTAINER" sh -c "psql -U \"\$POSTGRES_USER\" -d \"$1\" -tAc \"$2\""
}
count_in() { psql_db "$1" "select count(*) from $2" | tr -d '[:space:]'; }

cleanup() { psql_db postgres "DROP DATABASE IF EXISTS \"$DRILL_DB\"" > /dev/null || true; }
trap cleanup EXIT

echo "=== 演练目标 =============================================="
echo "备份文件: $DUMP ($(numfmt --to=iec "$(stat -c %s "$DUMP")"))"
echo "临时库  : $DRILL_DB（演练结束会删掉）"
echo

echo "=== ① 建空库并恢复 ========================================="
psql_db postgres "DROP DATABASE IF EXISTS \"$DRILL_DB\"" > /dev/null
psql_db postgres "CREATE DATABASE \"$DRILL_DB\""
# ⚠️ --no-owner：备份里的属主是生产库的角色，演练库没必要一致（也没有那个角色）
docker exec -i "$CONTAINER" sh -c "pg_restore -U \"\$POSTGRES_USER\" -d \"$DRILL_DB\" --no-owner" \
  < "$DUMP"
echo "✅ 恢复完成（pg_restore 没报错就是全量成功；它默认遇错即停）"
echo

echo "=== ② 逐表比对行数 ========================================="
printf '%-16s %10s %10s %s\n' "table" "prod" "drill" "结果"
FAILED=0
for table in $TABLES; do
  prod="$(count_in "$(docker exec "$CONTAINER" sh -c 'echo "$POSTGRES_DB"')" "$table")"
  drill="$(count_in "$DRILL_DB" "$table")"
  if [ "$prod" = "$drill" ]; then
    verdict="一致"
  else
    verdict="**不一致**"
    FAILED=1
  fi
  printf '%-16s %10s %10s %s\n' "$table" "$prod" "$drill" "$verdict"
done
echo

echo "=== ③ 关键结构也在 ========================================="
# 向量扩展 + 向量列：这两样缺了，恢复出来的库"看着有数据但检索不了"
psql_db "$DRILL_DB" "select extname from pg_extension order by 1" | tr '\n' ' '
echo "  ← 扩展（必须有 vector 与 pg_trgm）"
psql_db "$DRILL_DB" "select count(*) from information_schema.columns where table_name='chunks' and column_name='embedding'" | tr -d '[:space:]'
echo "  ← chunks.embedding 列（应是 1）"
psql_db "$DRILL_DB" "select count(*) from chunks where embedding is not null" | tr -d '[:space:]'
echo "  ← 带向量的块数（应与生产一致）"
echo

if [ "$FAILED" -ne 0 ]; then
  echo "FAIL restore-mismatch dump=$DUMP" >&2
  exit 1
fi
echo "PASS restore-drill dump=$DUMP"
