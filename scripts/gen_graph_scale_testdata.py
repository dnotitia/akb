#!/usr/bin/env python3
"""Generate a LARGE synthetic graph (vault `graph-scale`) for hands-on
viewport-culling / LOD development. Deterministic. Emits SQL on stdout; pipe to
the local postgres:

    python3 scripts/gen_graph_scale_testdata.py | \
      docker compose exec -T postgres psql -U akb -d akb -q

Structure tuned so LOD actually matters: ~8 collection clusters, a few
high-degree HUBS per cluster, many degree-1/2 LEAVES, intra- + inter-cluster
edges, mixed structural/associative relation types. Owner = the `demo` user.
"""
from __future__ import annotations

import random

DEMO_OWNER = "e8f72d3c-e5f1-4f9e-a954-fc1459bd7610"  # demo@demo.dev
VAULT = "graph-scale"
N_CLUSTERS = 8
DOCS_PER_CLUSTER = 50          # ~400 docs
HUBS_PER_CLUSTER = 3
EDGE_CAP = 600                 # the backend's limit*3=600 overview cap

STRUCTURAL = ["depends_on", "implements", "derived_from", "attached_to"]
ASSOCIATIVE = ["references", "related_to", "links_to"]

rnd = random.Random(42)


def esc(s: str) -> str:
    return s.replace("'", "''")


def uri(path: str) -> str:
    coll, _, base = path.rpartition("/")
    return f"akb://{VAULT}/coll/{coll}/doc/{base}"


print("BEGIN;")
print(f"DELETE FROM edges WHERE vault_id = (SELECT id FROM vaults WHERE name='{VAULT}');")
print(f"DELETE FROM documents WHERE vault_id = (SELECT id FROM vaults WHERE name='{VAULT}');")
print(f"DELETE FROM vault_access WHERE vault_id = (SELECT id FROM vaults WHERE name='{VAULT}');")
print(f"DELETE FROM vaults WHERE name='{VAULT}';")
print(
    f"INSERT INTO vaults (name, git_path, owner_id, status) "
    f"VALUES ('{VAULT}', '/data/vaults/{VAULT}.git', '{DEMO_OWNER}', 'active');"
)
print(
    f"INSERT INTO vault_access (vault_id, user_id, role, granted_by) "
    f"SELECT id, '{DEMO_OWNER}', 'owner', '{DEMO_OWNER}' FROM vaults WHERE name='{VAULT}';"
)

# ── nodes ──
docs: list[tuple[str, str]] = []          # (path, title)
hubs: list[str] = []                       # hub paths
by_cluster: list[list[str]] = []           # cluster -> [paths]
AREAS = [
    "auth", "billing", "search", "storage", "infra", "frontend", "agents", "data",
]
for c in range(N_CLUSTERS):
    area = AREAS[c % len(AREAS)]
    cluster_paths: list[str] = []
    for i in range(DOCS_PER_CLUSTER):
        path = f"{area}/{area}-{i:03d}.md"
        title = f"{area.title()} {'Hub' if i < HUBS_PER_CLUSTER else 'Note'} {i:03d}"
        docs.append((path, title))
        cluster_paths.append(path)
        if i < HUBS_PER_CLUSTER:
            hubs.append(path)
    by_cluster.append(cluster_paths)

vals = ",".join(
    f"((SELECT id FROM vaults WHERE name='{VAULT}'), '{esc(p)}', '{esc(t)}', 'manual', "
    f"jsonb_build_object('id', 'd-{abs(hash(p)) % (16**8):08x}'))"
    for p, t in docs
)
print(
    "INSERT INTO documents (vault_id, path, title, source, metadata) VALUES " + vals + ";"
)

# ── edges ── deterministic hub-and-spoke + cross-cluster, capped
edges: set[tuple[str, str, str]] = set()


def add(s: str, t: str, rel: str) -> None:
    if s == t or len(edges) >= EDGE_CAP:
        return
    edges.add((s, t, rel))


for c in range(N_CLUSTERS):
    paths = by_cluster[c]
    chubs = paths[:HUBS_PER_CLUSTER]
    leaves = paths[HUBS_PER_CLUSTER:]
    # hubs interlink (structural)
    for i, h in enumerate(chubs):
        for h2 in chubs[i + 1:]:
            add(h, h2, rnd.choice(STRUCTURAL))
    # every leaf attaches to exactly ONE in-cluster hub — maximizes the count
    # of DISTINCT connected nodes per edge (so the rendered graph clears the
    # >300-node cull threshold), a few also pick up a 2nd hub / leaf assoc.
    for leaf in leaves:
        add(leaf, rnd.choice(chubs), rnd.choice(STRUCTURAL))
        if rnd.random() < 0.30:
            add(leaf, rnd.choice(chubs), rnd.choice(ASSOCIATIVE))
# cross-cluster: hub→hub of other clusters (associative), makes the whole graph one component
for c in range(N_CLUSTERS):
    for h in by_cluster[c][:HUBS_PER_CLUSTER]:
        other = by_cluster[(c + 1) % N_CLUSTERS][0]
        add(h, other, "related_to")

erows = ",".join(
    f"((SELECT id FROM vaults WHERE name='{VAULT}'), '{esc(uri(s))}', '{esc(uri(t))}', "
    f"'{rel}', 'doc', 'doc', 'explicit')"
    for (s, t, rel) in edges
)
print(
    "INSERT INTO edges (vault_id, source_uri, target_uri, relation_type, "
    "source_type, target_type, kind) VALUES " + erows + ";"
)
print("COMMIT;")

import sys
print(
    f"-- generated: {len(docs)} docs, {len(edges)} edges, {N_CLUSTERS} clusters",
    file=sys.stderr,
)
