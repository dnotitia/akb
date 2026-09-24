"""The extension image cannot become a second way to move PostgreSQL.

`deploy/postgres/Dockerfile` builds AKB's PostgreSQL with `vchord_bm25`
compiled in, and the install paths build it. It is also a second place naming a PostgreSQL
image — and akb#619 was about exactly that failure mode: a reference that moves
the database while every manifest still reads the same. If the extension image
ever drifted off the pinned base, enabling BM25-on-index would quietly change
the PostgreSQL version at the same time, and the two changes would be
impossible to tell apart when something broke.

These assertions compare against the deployment manifest rather than against a
literal digest, so moving the pin does not mean editing a test too. Two copies
of a digest fall out of step the first time somebody forgets one.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _postgres_dockerfile() -> str:
    return (REPO / "deploy/postgres/Dockerfile").read_text()


def test_the_extension_image_builds_on_the_pinned_deployment_base():
    dockerfile = _postgres_dockerfile()
    base = re.search(r"^ARG PGVECTOR_IMAGE=(pgvector/\S+)", dockerfile, re.M)
    assert base, "확장 이미지가 pgvector 기반이 아니다"
    # Both the compile stage and the final image stand on it: the headers the
    # extension is compiled against belong to the server it runs in.
    assert dockerfile.count("FROM ${PGVECTOR_IMAGE}") == 2
    pinned = re.search(r"^\s*image:\s*(pgvector/\S+)",
                       (REPO / "deploy/k8s/postgres.yaml").read_text(), re.M)
    assert pinned, "배포 매니페스트에서 pgvector 이미지를 못 찾았다"
    assert base.group(1) == pinned.group(1), (
        "확장 이미지의 베이스가 배포 매니페스트의 핀과 다르다 — 확장을 켜는 것이 "
        "데이터베이스 버전까지 조용히 바꾸게 된다"
    )


def test_every_image_the_extension_build_pulls_is_pinned_by_digest():
    """A mutable tag in any stage reopens akb#619 through the side door."""
    dockerfile = _postgres_dockerfile()
    stages = set(re.findall(r"^FROM \S+ AS (\w+)", dockerfile, re.M))
    refs = re.findall(r"^ARG \w+_IMAGE=(\S+)", dockerfile, re.M)
    refs += [r for r in re.findall(r"^FROM (\S+)", dockerfile, re.M)
             if not r.startswith("${") and r not in stages]
    assert len(refs) >= 2, "이미지 참조를 못 찾았다"
    unpinned = [r for r in refs if "@sha256:" not in r]
    assert not unpinned, f"digest 없이 당기는 참조: {unpinned}"


def test_the_extension_is_compiled_from_pinned_source_with_its_fixes():
    """akb#679 and akb#684 are fixed by compiling the extension here, so what is compiled is pinned.

    The upstream tarball by sha256, the crates by a committed lockfile built
    `--locked` (0.3.0 ships none), and the patches applied exactly: a patch that
    no longer fits must fail the build rather than be skipped or bent.
    """
    dockerfile = _postgres_dockerfile()
    sha = re.search(r"^ARG VCHORD_BM25_SHA256=([0-9a-f]+)$", dockerfile, re.M)
    assert sha and len(sha.group(1)) == 64, "소스 tarball 의 sha256 핀이 없다"
    assert 'sha256sum -c -' in dockerfile
    assert "cargo build --locked" in dockerfile
    lockfile = REPO / "deploy/postgres/vchord_bm25/Cargo.lock"
    assert re.search(r'name = "pgrx"\nversion = "0\.16\.1"', lockfile.read_text()), lockfile
    patches = {p.name: p.read_text() for p in (REPO / "deploy/postgres/vchord_bm25").glob("*.patch")}
    fixes = {  # the files each fix changes
        "akb#679": ["src/segment/posting/serializer.rs"],
        # The length sum is counted the same way on every side: build, insert, VACUUM.
        "akb#684": ["src/segment/builder.rs", "src/index/insert.rs", "src/index/vacuum.rs"],
    }
    for issue, paths in fixes.items():
        for path in paths:
            assert any(f"+++ b/{path}" in text for text in patches.values()), f"{issue}: {path} 패치가 없다"
    for name, text in patches.items():
        assert "AGPL-3.0-only or Elastic-2.0" in text, name  # offered under the extension's own terms
    assert "set -eu" in dockerfile and "patch -p1 --forward --fuzz=0 --batch" in dockerfile
    assert re.search(r'"postgresql-server-dev-\$\{PG_MAJOR\}=\$\{PG_VERSION\}"', dockerfile), "헤더가 서버 버전에 고정되지 않았다"


def test_nothing_runs_the_upstream_prebuilt_extension():
    """The upstream 0.3.0 binary has the akb#679 and akb#684 defects.

    An install path or CI job that pulled the publisher's image would run, or
    test, an extension build no installation should have. The history in the
    changelog may name it; nothing that runs may.
    """
    runnable = [
        *(REPO / ".github").rglob("*.y*ml"),
        *_compose_files(),
        *(p for p in (REPO / "deploy").rglob("*") if p.is_file() and p.suffix != ".md"),
        *(p for p in (REPO / "scripts").rglob("*") if p.is_file()),
    ]
    pulls = [str(p.relative_to(REPO)) for p in runnable
             if "tensorchord/vchord_bm25-postgres" in p.read_text(errors="replace")]
    assert not pulls, pulls


def _compose_files() -> list[Path]:
    """Every tracked Compose file, found rather than listed.

    A list would go stale the first time somebody adds an overlay, and the
    reference this test exists to catch is exactly the one nobody remembered.
    """
    files = sorted(
        p for p in REPO.rglob("*compose*.y*ml")
        if ".git" not in p.parts and "node_modules" not in p.parts
    )
    assert files, "compose 파일을 하나도 못 찾았다"
    return files


def test_every_compose_reference_that_pulls_carries_a_digest():
    """akb#621: `minio/minio:latest` stopped resolving and nothing was red.

    A tag is a name the registry may repoint, and `:latest` is the most movable
    of them — so two developers can get different builds from one commit, and a
    repository that disappears is only noticed by whoever has no cache. Both
    failures are the same missing pin.

    This reads the files instead of repeating their literals, so moving a pin is
    one edit, not two that drift apart.
    """
    unpinned: list[str] = []
    for path in _compose_files():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            match = re.match(r"^\s*image:\s*(\S+)", line)
            if not match:
                continue
            ref = match.group(1)
            # An interpolated reference is the deployment's to pin: the file
            # names a variable, and its value is chosen where it is set.
            if ref.startswith("${"):
                continue
            if "@sha256:" not in ref:
                unpinned.append(f"{path.relative_to(REPO)}:{number} {ref}")
    assert not unpinned, (
        "digest 없이 당기는 compose 참조 — 레지스트리가 옮기면 조용히 바뀌거나 사라진다:\n  "
        + "\n  ".join(unpinned)
    )


def test_every_stack_runs_the_same_minio():
    """akb#621 asked for one MinIO version across development, eval and CI.

    Three compose files name it, so "named once" is enforced as agreement: every
    reference to the server is the same string, and so is every reference to the
    client. Before the fix they had drifted into two registries, and only one of
    them still served the image.
    """
    by_image: dict[str, set[str]] = {"minio/minio": set(), "minio/mc": set()}
    for path in _compose_files():
        for ref in re.findall(r"^\s*image:\s*(\S+)", path.read_text(), re.M):
            for name, seen in by_image.items():
                if re.search(rf"(^|/){re.escape(name)}[:@]", ref):
                    seen.add(ref)
    assert by_image["minio/minio"], "MinIO 서버 참조를 하나도 못 찾았다"
    for name, seen in by_image.items():
        assert len(seen) <= 1, f"{name} 참조가 스택마다 다르다: {sorted(seen)}"


def test_the_compose_install_paths_build_the_extension_image():
    """The default sparse shape needs vchord_bm25 in the server.

    AKB publishes no PostgreSQL image, so an install path that pulls the stock
    pgvector image gives every new database `posting`. The local stack and the
    CI runtime e2e build deploy/postgres instead, and carry no `image:` of their
    own that could drift from its pins.
    """
    import yaml

    for compose in ("docker-compose.yaml", "scripts/ci/dependency-compose.yaml"):
        path = REPO / compose
        postgres = yaml.safe_load(path.read_text())["services"]["postgres"]
        assert "image" not in postgres, f"{compose}: postgres 가 빌드 대신 이미지를 당긴다"
        context = (path.parent / postgres["build"]["context"]).resolve()
        assert context == (REPO / "deploy/postgres").resolve(), f"{compose}: {context}"


def test_the_published_all_in_one_does_not_bundle_the_extension():
    """The all-in-one is the one AKB image that is published (`dnseahorse/akb`).

    vchord_bm25 is AGPLv3 or ELv2. The install paths build deploy/postgres
    where they run, which is use; publishing an image that carries the
    extension is distribution of it, with the obligation the Dockerfile's
    licensing note describes. That is a decision of its own, not a side effect
    of making `vchord` the default, so a new all-in-one database gets `posting`.
    """
    dockerfile = (REPO / "deploy/all-in-one/Dockerfile").read_text()
    entrypoint = (REPO / "deploy/all-in-one/entrypoint.sh").read_text()
    assert "vchord" not in dockerfile.lower(), "공개 이미지가 vchord_bm25 를 싣는다"
    assert "vchord" not in entrypoint.lower(), "공개 이미지가 vchord_bm25 를 만든다"
