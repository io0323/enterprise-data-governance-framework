"""§14 Data Governance & AI: AI 資産のメタデータ (Card と版の情報).

学習データ、Embedding、ベクトル、RAG コーパス、モデルの重みは Data Plane の責務で、Control Plane には保存しない (§18.2)。
ここで扱うのは Card、Data Plane が計算した manifest のハッシュと件数、構成元の版、外部参照の URI だけ。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.enums import AiKind, DataClass, DatasetVersionStatus
from edgf.models import (DataAsset, DatasetCard, DatasetVersion, DatasetVersionSource, ModelCard, ModelUsage,
                         ModelVersion, PrivacyPurpose, RefDataSet, RefDataVersion, to_naive_utc)
from edgf.services import lineage
from edgf.services.mdm_rules import get_entities

MODEL = AiKind.MODEL.value
TRAINING_DATASET = AiKind.TRAINING_DATASET.value
RAG_CORPUS = AiKind.RAG_CORPUS.value
ALL_KINDS = tuple(k.value for k in AiKind)
DATA_KINDS = tuple(k.value for k in AiKind if k != AiKind.MODEL)  # Dataset Card を持つ種別
AI_TRAINING_PURPOSE = "ai_training"
# lineage_edges に反映するデータセット単位のエッジの job。これ以外の job のエッジは版の情報と対応しない
DATASET_JOB, MODEL_JOB, USAGE_JOB = "edgf.ai.dataset_version", "edgf.ai.model_version", "edgf.ai.usage"
DELETED = "deleted"
DATASET_CARD_FIELDS = ("purpose", "collection_source", "license", "known_biases", "constraints")
MODEL_CARD_FIELDS = ("intended_use", "out_of_scope_use", "metrics", "bias_evaluation_ref", "constraints")


class AiValidationError(ValueError):
    """入力の不備 (422)。"""


class AiStateError(ValueError):
    """現在の状態では実行できない操作 (409)。"""


def require_kind(asset: DataAsset, *kinds: str) -> None:
    if asset.data_class != DataClass.AI.value or asset.ai_kind not in kinds:
        raise AiValidationError(f"{asset.qualified_name} は ai_kind が {', '.join(kinds)} の資産ではありません")


def check_kind_change(db: Session, asset: DataAsset, data_class: str, ai_kind: str | None) -> None:
    """種別の変更を検証する。版や Card を持つ資産の種別を変えると、版の意味が変わるので拒否する。"""
    if ai_kind is not None and data_class != DataClass.AI.value:
        raise AiValidationError("ai_kind は data_class=ai の資産にだけ指定できます")
    if ai_kind == asset.ai_kind and data_class == asset.data_class:
        return
    used = (db.get(DatasetCard, asset.id) or db.get(ModelCard, asset.id)
            or db.scalar(select(DatasetVersion.id).where(DatasetVersion.asset_id == asset.id).limit(1))
            or db.scalar(select(ModelVersion.id).where(ModelVersion.asset_id == asset.id).limit(1)))
    if used:
        raise AiStateError("Card や版が登録済みの資産の種別は変更できません")


# ---------------------------------------------------------------- Card
def put_dataset_card(db: Session, asset: DataAsset, fields: Mapping[str, Any], actor: str) -> DatasetCard:
    require_kind(asset, *DATA_KINDS)
    purpose = fields.get("consent_purpose_id")
    if purpose and not db.get(PrivacyPurpose, purpose):
        raise AiValidationError(f"unknown purpose: {purpose}")
    card = db.get(DatasetCard, asset.id) or DatasetCard(asset_id=asset.id)
    for key, value in fields.items():
        setattr(card, key, value)
    card.updated_by = actor
    db.add(card)
    db.flush()
    return card


def put_model_card(db: Session, asset: DataAsset, fields: Mapping[str, Any], actor: str) -> ModelCard:
    require_kind(asset, MODEL)
    card = db.get(ModelCard, asset.id) or ModelCard(asset_id=asset.id)
    for key, value in fields.items():
        setattr(card, key, value)
    card.updated_by = actor
    db.add(card)
    db.flush()
    return card


def card_complete(db: Session, asset: DataAsset) -> bool:
    """Card の必須項目がすべて埋まっているか。PII を含む資産は同意の根拠も必須。"""
    if asset.ai_kind == MODEL:
        card = db.get(ModelCard, asset.id)
        return card is not None and all(getattr(card, f) for f in MODEL_CARD_FIELDS)
    card = db.get(DatasetCard, asset.id)
    if card is None or not all(getattr(card, f) for f in DATASET_CARD_FIELDS):
        return False
    return bool(card.consent_purpose_id) or not asset.contains_pii


# ---------------------------------------------------------------- 版の情報
def sources_of(db: Session, version: DatasetVersion) -> Sequence[DatasetVersionSource]:
    return db.scalars(select(DatasetVersionSource).where(DatasetVersionSource.dataset_version_id == version.id)
                      .order_by(DatasetVersionSource.qualified_name, DatasetVersionSource.id)).all()


def _check_source_version(db: Session, asset: DataAsset, kind: str, version: str) -> None:
    """kind ごとに、参照先の版が実在することを確かめる (§14.2 出所不明データの防止)。"""
    if kind == "reference_data":
        published = db.scalar(select(RefDataVersion.id).join(RefDataSet, RefDataVersion.set_id == RefDataSet.id).where(
            RefDataSet.asset_id == asset.id, RefDataVersion.status == "published",
            RefDataVersion.version == (int(version) if version.isdecimal() else -1)))
        if published is None:
            raise AiValidationError(f"{asset.qualified_name} に公開済みの Reference Data の版 {version} はありません")
    elif kind == "mdm":
        if not any(e.asset == asset.qualified_name for e in get_entities().values()):
            raise AiValidationError(f"{asset.qualified_name} に対応する MDM のエンティティ定義がありません")
    elif kind == "dataset_version":
        row = db.scalar(select(DatasetVersion).where(DatasetVersion.asset_id == asset.id, DatasetVersion.version == version))
        if row is None:
            raise AiValidationError(f"{asset.qualified_name} に Dataset Version {version} はありません")
        if row.status == DatasetVersionStatus.SUPERSEDED.value:
            raise AiStateError(f"{asset.qualified_name}@{version} は superseded のため構成元にできません")


def register_dataset_version(db: Session, asset: DataAsset, fields: Mapping[str, Any], sources: Sequence[Mapping[str, str]],
                             actor: str) -> DatasetVersion:
    require_kind(asset, *DATA_KINDS)
    if db.scalar(select(DatasetVersion.id).where(DatasetVersion.asset_id == asset.id,
                                                 DatasetVersion.version == fields["version"])):
        raise AiStateError(f"版 {fields['version']} は登録済みです (版は変更できません)")
    resolved: dict[tuple[str, str, str], DataAsset] = {}
    for src in sources:
        source = db.get(DataAsset, src["asset_id"])
        if source is None or source.lifecycle_state == DELETED:
            raise AiValidationError(f"構成元が Catalog にありません: {src['asset_id']} (§6.3 Metadata First)")
        if source.id == asset.id:
            raise AiValidationError("自分自身を構成元にはできません")
        _check_source_version(db, source, src["kind"], src["version"])
        resolved[(source.id, src["kind"], src["version"])] = source
    version = DatasetVersion(asset_id=asset.id, version=fields["version"], manifest_hash=fields["manifest_hash"],
                             record_count=fields["record_count"], external_uri=fields.get("external_uri"),
                             period_start=to_naive_utc(fields["period_start"]) if fields.get("period_start") else None,
                             period_end=to_naive_utc(fields["period_end"]) if fields.get("period_end") else None,
                             status=DatasetVersionStatus.DRAFT.value, registered_by=actor)
    db.add(version)
    db.flush()
    for (source_id, kind, source_version), source in resolved.items():
        db.add(DatasetVersionSource(dataset_version_id=version.id, asset_id=source_id, kind=kind, version=source_version,
                                    qualified_name=source.qualified_name))
        # 既存の impact_analysis と LIN-001 がそのまま使えるよう、データセット単位のエッジも反映する
        lineage.upsert_edge(db, source.qualified_name, asset.qualified_name, DATASET_JOB, run_id=version.id)
    db.flush()
    return version


def register_model_version(db: Session, asset: DataAsset, *, version: str, external_uri: str | None,
                           dataset_version_ids: Sequence[str], actor: str) -> ModelVersion:
    """学習データとの紐付けは必須 (§14.2 アンチパターン「モデルと学習データの紐付け欠落」)。"""
    require_kind(asset, MODEL)
    if db.scalar(select(ModelVersion.id).where(ModelVersion.asset_id == asset.id, ModelVersion.version == version)):
        raise AiStateError(f"版 {version} は登録済みです (版は変更できません)")
    datasets = []
    for dataset_id in dict.fromkeys(dataset_version_ids):
        dataset = db.get(DatasetVersion, dataset_id)
        if dataset is None:
            raise AiValidationError(f"Dataset Version が見つかりません: {dataset_id}")
        require_kind(db.get(DataAsset, dataset.asset_id), TRAINING_DATASET)
        if dataset.status == DatasetVersionStatus.SUPERSEDED.value:
            raise AiStateError(f"Dataset Version {dataset.version} は superseded のため学習に使えません")
        datasets.append(dataset)
    row = ModelVersion(asset_id=asset.id, version=version, external_uri=external_uri, registered_by=actor, datasets=datasets)
    db.add(row)
    db.flush()
    for dataset in datasets:
        lineage.upsert_edge(db, db.get(DataAsset, dataset.asset_id).qualified_name, asset.qualified_name, MODEL_JOB,
                            run_id=row.id)
    return row


def add_usage(db: Session, version: ModelVersion, *, kind: str, target: str, target_asset_id: str | None,
              description: str, actor: str) -> ModelUsage:
    """利用先を記録する。Catalog の資産なら下流のエッジにも反映する (消去やリコールの影響範囲に入る)。"""
    if target_asset_id:
        target_asset = db.get(DataAsset, target_asset_id)
        if target_asset is None:
            raise AiValidationError(f"利用先の資産が見つかりません: {target_asset_id}")
        model = db.get(DataAsset, version.asset_id)
        if target_asset.id == model.id:
            raise AiValidationError("モデル自身を利用先にはできません")
        lineage.upsert_edge(db, model.qualified_name, target_asset.qualified_name, USAGE_JOB, run_id=version.id)
    usage = ModelUsage(model_version_id=version.id, kind=kind, target=target, target_asset_id=target_asset_id,
                       description=description, created_by=actor)
    db.add(usage)
    db.flush()
    return usage
