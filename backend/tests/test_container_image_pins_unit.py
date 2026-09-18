"""The optional extension image cannot become a second way to move PostgreSQL.

`deploy/postgres/Dockerfile` builds AKB's PostgreSQL with `vchord_bm25` added.
It is optional, but it is also a second place naming a PostgreSQL image — and
akb#619 was about exactly that failure mode: a reference that moves the
database while every manifest still reads the same. If the extension image ever
drifted off the pinned base, enabling BM25-on-index would quietly change the
PostgreSQL version at the same time, and the two changes would be impossible to
tell apart when something broke.

These assertions compare against the deployment manifest rather than against a
literal digest, so moving the pin does not mean editing a test too. Two copies
of a digest fall out of step the first time somebody forgets one.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_the_extension_image_builds_on_the_pinned_deployment_base():
    dockerfile = (REPO / "deploy/postgres/Dockerfile").read_text()
    base = re.search(r"^FROM (pgvector/\S+)", dockerfile, re.M)
    assert base, "확장 이미지가 pgvector 기반이 아니다"
    pinned = re.search(r"^\s*image:\s*(pgvector/\S+)",
                       (REPO / "deploy/k8s/postgres.yaml").read_text(), re.M)
    assert pinned, "배포 매니페스트에서 pgvector 이미지를 못 찾았다"
    assert base.group(1) == pinned.group(1), (
        "확장 이미지의 베이스가 배포 매니페스트의 핀과 다르다 — 확장을 켜는 것이 "
        "데이터베이스 버전까지 조용히 바꾸게 된다"
    )


def test_every_stage_of_the_extension_image_is_pinned_by_digest():
    """A mutable tag in either stage reopens akb#619 through the side door."""
    dockerfile = (REPO / "deploy/postgres/Dockerfile").read_text()
    refs = re.findall(r"^(?:FROM|ARG \w+=)\s*(\S+)", dockerfile, re.M)
    refs = [r for r in refs if not r.startswith("${")]
    assert refs, "이미지 참조를 못 찾았다"
    unpinned = [r for r in refs if "@sha256:" not in r]
    assert not unpinned, f"digest 없이 당기는 참조: {unpinned}"
