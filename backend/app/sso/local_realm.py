"""The installation's own realm, offered as a login option.

Every other provider is a Keycloak identity provider: registering one creates an
object in the realm, and `KeycloakProviderControl.list_providers` reads the
catalog back out of Keycloak. There is no provider table on this side.

This kind materialises nothing, which is the whole point of it -- people sign in
against the realm the installation already owns instead of being brokered
somewhere else. So it is not in `app.sso.registry`, which is the registry OF
Keycloak representations; it is a setting, projected into the same catalog at the
one place the catalog is read for the browser.

It exists because an `sso` installation with no external identity provider had no
way in at all: `providers` was empty, the retired provider-less route answers 410,
registering the realm as its own provider is refused (correctly -- it is an
authorize loop), and no realm-creating credential survives the install. See
dnotitia/akb#446.

The alias is RESERVED. A Keycloak identity provider may not take it, because the
whole discrimination below rests on the two kinds never sharing a name.
"""

from __future__ import annotations

PROVIDER_TYPE = "local-realm"
ALIAS = "local"


def is_local_alias(alias: str) -> bool:
    return alias == ALIAS
