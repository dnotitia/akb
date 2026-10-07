"""The realm mark that keeps a brokered account off the realm's own password.

Someone who arrives through an identity provider has to lose this installation
when that provider stops vouching for them. A password in this realm would
outlive that, and Keycloak offers one to anyone already signed in: the
application-initiated action ``kc_action=UPDATE_PASSWORD``, which is also what
the account console's "set up password" starts.

So every identity provider grants one realm role to the accounts it brings, and
the realm's password-accepting flows refuse an account holding that role. The
mark is a role rather than something read off the link itself because Keycloak
26.7 has no "is this account linked to a provider" condition; a role is also
something the account's owner cannot change about themselves.

Only the representation lives here. The realm side (the role and the guarded
flows) is installed by the bundled-realm bootstrap; the provider control writes
the mapper on every provider it creates once the realm carries the role.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


BROKERED_ACCOUNT_ROLE = "akb-brokered-account"
BROKERED_ACCOUNT_ROLE_DESCRIPTION = (
    "Granted by every identity provider to the accounts it brings. The realm refuses a password for an account holding it."
)
BROKERED_ACCOUNT_MAPPER_NAME = "akb-brokered-account"
# Keycloak's hardcoded-role mapper is provider-neutral despite the id prefix.
_HARDCODED_ROLE_MAPPER = "oidc-hardcoded-role-idp-mapper"


def identity_provider_mapper(alias: str) -> dict[str, Any]:
    return {
        "name": BROKERED_ACCOUNT_MAPPER_NAME,
        "identityProviderAlias": alias,
        "identityProviderMapper": _HARDCODED_ROLE_MAPPER,
        # FORCE grants the role again on every brokered login, so an account
        # linked before the mapper existed is marked the next time it arrives,
        # whatever sync mode the provider itself uses.
        "config": {"role": BROKERED_ACCOUNT_ROLE, "syncMode": "FORCE"},
    }


def identity_provider_mapper_matches(actual: Mapping[str, object], alias: str) -> bool:
    expected = identity_provider_mapper(alias)
    config = actual.get("config")
    return (
        actual.get("name") == expected["name"]
        and actual.get("identityProviderAlias") == alias
        and actual.get("identityProviderMapper") == expected["identityProviderMapper"]
        and isinstance(config, dict)
        and all(config.get(key) == value for key, value in expected["config"].items())
    )
