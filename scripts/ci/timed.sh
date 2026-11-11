#!/bin/sh
# テストのコマンドを実行し、所要時間を job summary に書く。ジョブの上限 (JOB_TIMEOUT_MINUTES) の
# CI_WARN_RATIO (既定 0.6) を超えたら ::warning:: で警告する。警告だけで、失敗にはしない。
#
#   JOB_TIMEOUT_MINUTES=40 sh scripts/ci/timed.sh "pytest (PostgreSQL)" pytest -q ...
#
# コマンドの終了コードをそのまま返す。
set -u
label="$1"
shift
: "${JOB_TIMEOUT_MINUTES:?JOB_TIMEOUT_MINUTES (ジョブの timeout-minutes と同じ値) を指定してください}"
ratio="${CI_WARN_RATIO:-0.6}"
start=$(date +%s)
"$@"
status=$?
elapsed=$(( $(date +%s) - start ))
limit=$(( JOB_TIMEOUT_MINUTES * 60 ))
threshold=$(awk -v l="$limit" -v r="$ratio" 'BEGIN { printf "%d", l * r }')
percent=$(awk -v e="$elapsed" -v l="$limit" 'BEGIN { printf "%.0f", e * 100 / l }')
minutes=$(awk -v e="$elapsed" 'BEGIN { printf "%.1f", e / 60 }')
line="| ${label} | ${minutes} 分 | ${JOB_TIMEOUT_MINUTES} 分 | ${percent}% |"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### 所要時間"
    echo ""
    echo "| 対象 | 所要時間 | ジョブの上限 | 上限に対する割合 |"
    echo "|---|---|---|---|"
    echo "$line"
  } >> "$GITHUB_STEP_SUMMARY"
fi
echo "$label: ${minutes} 分 (ジョブの上限 ${JOB_TIMEOUT_MINUTES} 分の ${percent}%)"
if [ "$elapsed" -gt "$threshold" ]; then
  echo "::warning title=CI の所要時間::${label} が ${minutes} 分かかりました。ジョブの上限 ${JOB_TIMEOUT_MINUTES} 分の ${percent}% で、警告の閾値 (${ratio}) を超えています。上限の見直しかテストの短縮を検討してください (docs/operations/ci.md)"
fi
exit "$status"
