"""Import the supplied Yahoo opponent matrix with verified team-ID reconciliation."""
from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass
from hashlib import sha256
import io
from pathlib import Path
import re
import unicodedata


@dataclass(frozen=True)
class MatchupSchedule:
    pairings: dict[int, tuple[tuple[str, str], ...]]
    team_names: dict[str, str]
    source_sha256: str
    encoding: str
    aliases: dict[str, str]

    def opponent(self, week: int, team_id: str) -> str:
        for a, b in self.pairings[week]:
            if team_id in (a, b):
                return b if team_id == a else a
        raise KeyError((week, team_id))


def _key(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold().replace('’', "'").strip()


def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ('utf-8-sig', 'cp1252'):
        try:
            value = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if '\ufffd' in value:
            raise ValueError('Matchup CSV contains replacement characters; resolve the source encoding')
        return value, encoding
    raise ValueError('Matchup CSV is neither UTF-8 nor Windows-1252')


def load_matchups(path: Path, verified_teams: dict[str, str]) -> MatchupSchedule:
    """Names in both header and cells are matched to saved Yahoo IDs, never column indices."""
    if len(verified_teams) != 10 or len(set(verified_teams.values())) != 10:
        raise ValueError('Exactly ten distinct verified Yahoo team IDs/names are required')
    raw = path.read_bytes()
    decoded, encoding = _decode(raw)
    rows = list(csv.reader(io.StringIO(decoded, newline='')))
    if len(rows) != 19 or not rows or len(rows[0]) != 11 or _key(rows[0][0]) != 'wk':
        raise ValueError('Matchup CSV must have one header and 18 eleven-column weeks')
    names = {_key(name): team_id for team_id, name in verified_teams.items()}
    if len(names) != 10:
        raise ValueError('Verified Yahoo team names are ambiguous after normalization')
    supplied = {_key(cell) for row in rows for cell in row[1:]}
    unknown = supplied - set(names)
    aliases = {}
    if unknown:
        # A damaged emoji name is resolvable only when it is the sole unknown
        # token and precisely one verified team is absent from all readable names.
        missing = set(verified_teams) - {names[n] for n in supplied & set(names)}
        if len(unknown) != 1 or len(missing) != 1 or not re.fullmatch(r'[?\s]+', next(iter(unknown))):
            raise ValueError(f'Ambiguous matchup team name(s): {sorted(unknown)}; unmatched Yahoo IDs: {sorted(missing)}')
        token, team_id = next(iter(unknown)), next(iter(missing))
        names[token] = team_id
        aliases[token] = team_id
    headers = [names.get(_key(cell)) for cell in rows[0][1:]]
    if None in headers or len(set(headers)) != 10 or set(headers) != set(verified_teams):
        raise ValueError('Matchup columns do not identify ten distinct verified Yahoo teams')
    pairings = {}
    pair_counts: Counter[tuple[str, str]] = Counter()
    for expected_week, row in enumerate(rows[1:], 1):
        if len(row) != 11 or row[0].strip() != str(expected_week):
            raise ValueError(f'Expected exactly ten opponent cells for Yahoo Week {expected_week}')
        opponents = {team: names.get(_key(cell)) for team, cell in zip(headers, row[1:])}
        if None in opponents.values() or any(team == opponent for team, opponent in opponents.items()):
            raise ValueError(f'Unknown or self opponent in Week {expected_week}')
        if any(opponents.get(opponent) != team for team, opponent in opponents.items()):
            raise ValueError(f'Nonreciprocal matchup in Week {expected_week}')
        pairs = tuple(sorted({tuple(sorted((team, opponent))) for team, opponent in opponents.items()}))
        if len(pairs) != 5 or len({t for pair in pairs for t in pair}) != 10:
            raise ValueError(f'Week {expected_week} does not have five unique matchups')
        pairings[expected_week] = pairs
        pair_counts.update(pairs)
    if len(pair_counts) != 45 or set(pair_counts.values()) != {2}:
        raise ValueError('Every pair of Yahoo teams must meet exactly twice in Weeks 1–18')
    return MatchupSchedule(pairings, verified_teams, sha256(raw).hexdigest(), encoding, aliases)
