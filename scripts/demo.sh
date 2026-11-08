#!/usr/bin/env bash
# 簡易デモ: ローカル起動中の API にサンプルデータを投入する。
# 前提: dev モード (EDGF_AUTH_MODE=dev。docker-compose.dev.yml を使うか、ローカルで環境変数を指定)。
# JWT で呼び出す手順は README の「認証」を参照。
set -euo pipefail
B=${EDGF_URL:-http://localhost:8000}/api/v1
# j <path> <actor> <roles> <json>
j() { curl -sf -H 'Content-Type: application/json' -H "X-Actor: $2" -H "X-Roles: $3" -H "X-Domains: *" \
        -X POST "$B$1" -d "$4"; echo; }
j /domains committee-chair committee '{"name":"sales","owner":"sales-head","description":"営業ドメイン"}'
j /assets steward1 steward '{"qualified_name":"sales.orders","name":"受注","description":"受注トランザクション","domain":"sales","data_class":"transactional","sensitivity":"confidential","owner":"owner1","steward":"steward1","is_cde":true,"retention_days":2555,"freshness_sla_hours":24,"schema":[{"name":"order_id"},{"name":"email","pii":true}],"controls":["encryption_at_rest","encryption_in_transit","masking"]}'
curl -sf -H "X-Actor: analyst1" "$B/kpi"; echo
