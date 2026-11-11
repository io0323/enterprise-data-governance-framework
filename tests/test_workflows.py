"""CI のワークフローのファイルの検査 (GitHub は重複したキーを拒否するが、PyYAML は黙って後勝ちにする)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))


class _StrictLoader(yaml.SafeLoader):
    pass


def _no_duplicates(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    duplicated = sorted({k for k in keys if keys.count(k) > 1}, key=str)
    if duplicated:
        raise yaml.constructor.ConstructorError(None, None, f"重複したキー: {duplicated}", node.start_mark)
    return loader.construct_mapping(node, deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_has_no_duplicate_keys(path):
    doc = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)  # noqa: S506  # nosec B506 - SafeLoader の派生
    assert doc["jobs"]


def test_detector_catches_duplicates():
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.load("jobs:\n  a: 1\n  a: 2\n", Loader=_StrictLoader)  # noqa: S506  # nosec B506
