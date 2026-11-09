#!/bin/sh
# 負荷試験の対象を起動する。dev モードで認証の差を除く (JWT の検証は 1 リクエストあたり 1ms 未満で、結果にほぼ影響しない)。
: "${EDGF_DATABASE_URL:?EDGF_DATABASE_URL を指定してください}"
export EDGF_ENV=test EDGF_AUTH_MODE=dev EDGF_LOG_LEVEL=WARNING
exec "${PYTHON:-python}" -m uvicorn edgf.main:app --host 127.0.0.1 --port "${EDGF_PERF_PORT:-8100}" --workers "${EDGF_PERF_WORKERS:-4}" --no-access-log
