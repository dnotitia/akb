"""Shared activity predicates for Native, Git and migrated history."""
from datetime import UTC, datetime, timedelta


def parse_activity_since(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp


def matches_author(needle: str | None, author: str, agent: str) -> bool:
    return not needle or needle.lower() in author.lower() or needle.lower() in agent.lower()


def commit_metadata(body: str) -> dict[str, str]:
    metadata = {}
    for line in body.split("\n"):
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip().lower()] = value.strip()
    return metadata


def git_activity_dates(since: str | None, until: datetime | None) -> dict[str, str] | None:
    if until is None:
        return {"since": since} if since else {}
    # Git compares inclusive whole commit seconds. Round an exclusive end
    # down to the last admissible second and an inclusive start up.
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    second = timedelta(seconds=1)
    last = (until - epoch) // second - int(until.microsecond == 0)
    if last < 0:  # Git commit timestamps are unsigned.
        return None
    options = {"until": f"@{last} +0000"}
    if since:
        start = parse_activity_since(since)
        first = (start - epoch) // second + int(start.microsecond != 0)
        options["since_as_filter"] = f"@{max(0, first)} +0000"
    return options
