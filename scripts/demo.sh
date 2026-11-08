#!/usr/bin/env bash
# 簡易デモ: ローカル起動中のAPIにサンプルデータを投入する
set -euo pipefail
B=${EDGF_URL:-http://localhost:8000}/api/v1
j() { curl -sf -H 'Content-Type: application/json' -H "X-Actor: ${2:-demo}" -X POST "$B$1" -d "$3"; echo; }
j /domains demo '{"name":"sales","owner":"sales-head","description":"営業ドメイン"}'
j /assets demo '{"qualified_name":"sales.orders","name":"受注","description":"受注トランザクション","domain":"sales","data_class":"transactional","sensitivity":"confidential","owner":"owner1","steward":"steward1","is_cde":true,"retention_days":2555,"freshness_sla_hours":24,"schema":[{"name":"order_id"},{"name":"email","pii":true}],"controls":["encryption_at_rest","encryption_in_transit","masking"]}'
curl -sf "$B/kpi"; echo
