"""HTTP test actors for the disposable repository runtime."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, repr=False)
class Actor:
    id: UUID
    username: str
    jwt: str
    pat: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.pat}"}
