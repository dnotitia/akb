-- PG first-boot init: install pgvector extension before backend boots.
-- The backend's bundled init.sql doesn't include this line, and the
-- pgvector driver's `register_vector(conn)` call fails ("unknown type:
-- public.vector") before it ever reaches the CREATE EXTENSION inside
-- ensure_collection -- causing a circular dependency that leaves every
-- chunk stuck retrying with vector_last_error = 'unknown type: public.vector'.
--
-- The cluster's akb-init-sql ConfigMap already contains this line; this
-- file is the docker-compose equivalent.
CREATE EXTENSION IF NOT EXISTS vector;
