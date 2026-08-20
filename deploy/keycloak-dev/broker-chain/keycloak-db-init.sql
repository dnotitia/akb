-- The broker Keycloak's own database, beside AKB's. One PostgreSQL process
-- serves both so the fixture stays three containers, and both are on the
-- same tmpfs volume that the runner destroys on teardown.
CREATE DATABASE keycloak OWNER akb;
