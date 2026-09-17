"""Tenant `/stats` — a cached inventory snapshot on its own listener.

Three modules, one seam each:

* :mod:`app.stats.sampler`  — computes the snapshot on a timer and caches it.
* :mod:`app.stats.listener` — the second uvicorn socket that serves the cache.
* ``schema_v1.json`` / ``golden_v1.json`` — the wire contract this repo owns.
  The control plane vendors the schema at a pinned version; the golden fixture
  is the shape both sides test against.

Nothing here is a metrics endpoint. There is no ``prometheus_client``, no
registry, no exposition format — a JSON body, and it is served from cache so
that polling it cannot become database load.
"""
