"""Offline, read-only Codex session metadata scanner and approximate reset planner.
No imports or functions in this module initiate network/CLI requests.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
from statistics import median
from typing import Optional

UTC = timezone.utc
WEEK = timedelta(days=7)
FIVE = timedelta(hours=5)


def local_time(dt: Optional[datetime]) -> str:
    return dt.astimezone().strftime("%d %b · %H:%M") if dt else "Not reported"


def parse_dt(value) -> Optional[datetime]:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def safe_percent(v) -> Optional[float]:
    try:
        x = float(v)
        return max(0., min(100., x)) if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def choose_bucket(rate_limits: dict) -> dict:
    """Prefer the standard Codex rate-limit bucket, not a different product bucket."""
    if not isinstance(rate_limits, dict):
        return {}
    if 'primary' in rate_limits or 'secondary' in rate_limits:
        return rate_limits
    grouped = (rate_limits.get('rate_limits_by_limit_id') or
               rate_limits.get('rateLimitsByLimitId') or {})
    if isinstance(grouped, dict):
        b = grouped.get('codex') or grouped.get('codex_cli')
        if isinstance(b, dict):
            return b
    return {}


@dataclass(frozen=True)
class Observation:
    ts: datetime
    five_used: Optional[float]
    week_used: Optional[float]
    five_end: Optional[datetime]
    week_end: Optional[datetime]


@dataclass
class Usage:
    folder: str
    observations: list[Observation] = field(default_factory=list)
    model: Optional[str] = None
    scanned_files: int = 0
    malformed_lines: int = 0
    daily_rate: Optional[float] = None
    ratio: Optional[float] = None
    observed_days: int = 0
    status: str = ""

    @property
    def latest(self) -> Optional[Observation]:
        return self.observations[-1] if self.observations else None


def codex_folder_default() -> Path:
    return Path(os.environ.get('CODEX_HOME', '')).expanduser() if os.environ.get('CODEX_HOME') else Path.home() / '.codex'


def scan_codex(folder: str | Path, now: Optional[datetime] = None) -> Usage:
    now = now or datetime.now(UTC)
    root = Path(folder).expanduser()
    result = Usage(folder=str(root))
    if not root.is_dir():
        result.status = "Codex folder not found"
        return result
    candidates = []
    for dirname in ('sessions', 'archived_sessions'):
        d = root / dirname
        if d.exists():
            try:
                for f in d.rglob('*.jsonl'):
                    try:
                        stat = f.stat()
                        if stat.st_mtime > (now - timedelta(days=30)).timestamp() and stat.st_size <= 200_000_000:
                            candidates.append((stat.st_mtime, f))
                    except OSError:
                        pass
            except OSError:
                pass
    # Most recently touched files first. Hard limits protect UI responsiveness.
    candidates.sort(reverse=True)
    candidates = candidates[:280]
    seen = set()
    bytes_seen = 0
    read_budget = 150_000_000
    models = Counter()
    observations = []
    for _, path in candidates:
        if bytes_seen >= read_budget:
            break
        try:
            result.scanned_files += 1
            with path.open('rb') as stream:
                for line in stream:
                    bytes_seen += len(line)
                    if bytes_seen > read_budget:
                        break
                    # Never parse or collect conversations/tool outputs; only metadata record types.
                    if (b'"token_count"' not in line and b'"turn_context"' not in line):
                        continue
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                        result.malformed_lines += 1
                        continue
                    typ = row.get('type')
                    payload = row.get('payload')
                    if not isinstance(payload, dict):
                        continue
                    if typ == 'turn_context':
                        model = payload.get('model')
                        if isinstance(model, str) and len(model) < 80 and model.lower() != 'unknown':
                            models[model] += 1
                        continue
                    if typ != 'event_msg' or payload.get('type') != 'token_count':
                        continue
                    ts = parse_dt(row.get('timestamp'))
                    if ts is None or ts > now + timedelta(minutes=5) or ts < now - timedelta(days=32):
                        continue
                    limits = choose_bucket(payload.get('rate_limits') or {})
                    primary = limits.get('primary') or {}
                    secondary = limits.get('secondary') or {}
                    if not isinstance(primary, dict):
                        primary = {}
                    if not isinstance(secondary, dict):
                        secondary = {}
                    five = safe_percent(primary.get('used_percent', primary.get('usedPercent')))
                    week = safe_percent(secondary.get('used_percent', secondary.get('usedPercent')))
                    if five is None and week is None:
                        continue
                    f_end = parse_dt(primary.get('resets_at', primary.get('resetsAt')))
                    w_end = parse_dt(secondary.get('resets_at', secondary.get('resetsAt')))
                    identity = (ts, five, week, f_end, w_end)
                    if identity not in seen:
                        observations.append(Observation(ts, five, week, f_end, w_end))
                        seen.add(identity)
        except (OSError, PermissionError):
            continue
    result.observations = sorted(observations, key=lambda o: o.ts)
    result.model = models.most_common(1)[0][0] if models else None
    result.daily_rate, result.ratio, result.observed_days = estimate_usage(result.observations, now)
    if not result.observations:
        result.status = 'No usable local rate-limit snapshots'
    else:
        hours = (now - result.latest.ts).total_seconds() / 3600
        result.status = ('Recently observed' if hours <= 6 else
                         'Snapshot is stale' if hours > 24 else 'Previously observed')
    return result


def same_window(a: Optional[datetime], b: Optional[datetime]) -> bool:
    return a is not None and b is not None and abs((a - b).total_seconds()) <= 90


def estimate_usage(items: list[Observation], now: datetime):
    """Use change in account-limit percentages, not token counts, as allowance proxy."""
    if len(items) < 2:
        return None, None, 0
    per_day = defaultdict(float)
    ratios = []
    newest = items[-1].ts
    valid_start = max(now - timedelta(days=10), newest - timedelta(days=10))
    useful = [o for o in items if o.ts >= valid_start]
    for old, new in zip(useful, useful[1:]):
        elapsed = (new.ts - old.ts).total_seconds()
        if not 0 < elapsed <= 48 * 3600:
            continue
        if same_window(old.week_end, new.week_end) and old.week_used is not None and new.week_used is not None:
            delta_w = new.week_used - old.week_used
            if 0.02 < delta_w <= 65:
                # Spreading multi-day changes avoids assigning 48h of work to one day.
                buckets = []
                cursor = old.ts
                while cursor < new.ts:
                    local = cursor.astimezone()
                    next_midnight_local = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), local.tzinfo)
                    segment_end = min(new.ts, next_midnight_local.astimezone(UTC))
                    if segment_end <= cursor:
                        break
                    buckets.append((local.date(), (segment_end-cursor).total_seconds()/elapsed))
                    cursor = segment_end
                for day, fraction in buckets:
                    per_day[day] += delta_w * fraction
                if (same_window(old.five_end, new.five_end) and old.five_used is not None
                        and new.five_used is not None):
                    delta_f = new.five_used - old.five_used
                    if 0.1 < delta_f <= 90 and delta_w >= 0.1:
                        r = delta_f / delta_w
                        if 0.15 <= r <= 30:
                            ratios.append(r)
    # Use actual elapsed calendar days when there is a dense history; small samples are labelled estimates.
    rate = None
    if per_day:
        days = sorted(per_day)
        window_days = max(1, min(7, (days[-1] - days[0]).days + 1))
        rate = round(min(100., sum(per_day.values()) / window_days), 2)
    return rate, round(median(ratios), 2) if ratios else None, len(per_day)


@dataclass
class Settings:
    banked: int = 0
    expiry: str = ''
    daily_override: Optional[float] = None
    ratio_override: Optional[float] = None
    folder: str = ''
    demo: bool = False

    @classmethod
    def load(cls, path: Path) -> 'Settings':
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            allowed = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
            s = cls(**allowed)
            s.banked = min(9, max(0, int(s.banked)))
            return s
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return cls()

    def save(self, path: Path):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            import dataclasses
            path.write_text(json.dumps(dataclasses.asdict(self), indent=2), encoding='utf-8')
        except OSError:
            pass


def demo_usage(now: Optional[datetime] = None) -> Usage:
    """Synthetic, explicitly labelled display data; never masquerades as real usage."""
    now = now or datetime.now(UTC)
    obs = []
    w_end = now + timedelta(days=3, hours=8)
    for i in range(48):
        ts = now - timedelta(hours=(48-i)*4)
        five_end = ts + timedelta(hours=3)
        week_used = max(0, min(94, 19 + (i*1.13)))
        five_used = (i * 6.7) % 86
        obs.append(Observation(ts, five_used, week_used, five_end, w_end))
    obs.append(Observation(now - timedelta(minutes=12), 68, 73, now + timedelta(hours=2, minutes=48), w_end))
    return Usage(folder='DEMO — synthetic data', observations=obs, model='gpt-6-sol',
                 scanned_files=0, daily_rate=11.4, ratio=3.7, observed_days=7,
                 status='DEMO ONLY — not your account')


@dataclass
class PlanState:
    week_used: float
    five_used: float
    week_end: int  # zero => start on next demand
    five_end: int
    served: float = 0.
    discarded: float = 0.
    taken: tuple[int, ...] = ()


@dataclass
class Plan:
    mode: str
    start: datetime
    daily_target: float
    ratio: float
    banked: int
    chosen: list[datetime]
    daily_rows: list[dict]
    events: list[dict]
    demand_total: float
    served_total: float
    baseline_total: float
    discarded: float
    explanation: str

    @property
    def gained(self) -> float:
        return max(0., self.served_total - self.baseline_total)


def hour_grid(start: datetime, n_days: int = 21):
    t = start.replace(minute=0, second=0, microsecond=0)
    if t < start:
        t += timedelta(hours=1)
    return [t + timedelta(hours=i) for i in range(n_days*24)]


def demand_for(t: datetime, daily: float, mode: str) -> float:
    lt = t.astimezone()
    weekday = lt.weekday()
    h = lt.hour
    if mode == 'spiky':
        if weekday in (1, 4):  # two large planned workdays per week
            if h in (10, 11, 12, 13, 14, 15, 16, 17, 18):
                return daily * 3.1 / 9
        elif weekday < 5 and h in (11, 12, 13, 14):
            return daily * .32 / 4
        elif weekday >= 5 and h in (12, 13):
            return daily * .18 / 2
        return 0.
    if weekday < 5 and h in range(9, 19):
        return daily * 1.05 / 10
    if weekday >= 5 and h in range(11, 16):
        return daily * .45 / 5
    return 0.


def initial_state(obs: Optional[Observation], start: datetime) -> PlanState:
    if not obs:
        return PlanState(0., 0., 0, 0)
    epoch = int(start.timestamp())
    wend = int(obs.week_end.timestamp()) if obs.week_end else 0
    fend = int(obs.five_end.timestamp()) if obs.five_end else 0
    week = obs.week_used or 0.
    five = obs.five_used or 0.
    # Expired windows are re-anchored on the next simulated request.
    if wend <= epoch:
        wend, week = 0, 0.
    if fend <= epoch:
        fend, five = 0, 0.
    return PlanState(week, five, wend, fend)


def advance(s: PlanState, hour: int, want: float, ratio: float, redeem: bool = False) -> PlanState:
    wu, fu, we, fe = s.week_used, s.five_used, s.week_end, s.five_end
    discarded = s.discarded
    taken = s.taken
    if redeem:
        # A saved reset must refresh a partially used eligible window.
        discarded += max(0., 100 - wu)
        wu, fu, we, fe = 0., 0., 0, 0
        taken = taken + (hour,)
    if want > 0:
        if not we or hour >= we:
            wu, we = 0., hour + 7*24*3600
        if not fe or hour >= fe:
            fu, fe = 0., hour + 5*3600
        can_serve = min(want, max(0., 100-wu), max(0., 100-fu) / ratio)
        wu += can_serve
        fu += can_serve * ratio
    else:
        can_serve = 0.
    return PlanState(wu, fu, we, fe, s.served + can_serve, discarded, taken)


def optimize(observation: Optional[Observation], now: datetime, daily: float,
             ratio: float, banked: int, mode: str, expiry: Optional[date] = None,
             days: int = 21, width: int = 36) -> Plan:
    """Beam-search over candidate banked reset hours; simulate both rolling budget windows.

    This is a scenario optimizer, not an account-level entitlement model. Relative costs
    between windows are learned from observed deltas or are user-supplied assumptions.
    """
    daily = max(0., min(100., float(daily)))
    ratio = max(0.15, min(30., float(ratio)))
    banked = max(0, min(9, int(banked)))
    grid = hour_grid(now, days)
    demand = [demand_for(t, daily, mode) for t in grid]
    epoch_grid = [int(t.timestamp()) for t in grid]
    init = initial_state(observation, now)
    basestate = init
    for hour, want in zip(epoch_grid, demand):
        basestate = advance(basestate, hour, want, ratio)
    if not banked:
        chosen_state = basestate
    else:
        beams = {0: [init]}
        for i, (t, h, want) in enumerate(zip(grid, epoch_grid, demand)):
            candidate = defaultdict(dict)
            can_reset_here = (i % 2 == 0 and want > 0 and
                              (expiry is None or t.astimezone().date() <= expiry))
            for used, states in beams.items():
                for s in states:
                    variants = [(used, advance(s, h, want, ratio))]
                    if (used < banked and can_reset_here and (s.week_used > 1 or s.five_used > 2)):
                        variants.append((used + 1, advance(s, h, want, ratio, True)))
                    for n_used, result in variants:
                        # Discretized dedup allows diverse reset phases without huge state growth.
                        key = (round(result.week_used / 4), round(result.five_used / 6),
                               result.week_end // 10800, result.five_end // 3600)
                        score = result.served - result.discarded * .015 - n_used * .6
                        prev = candidate[n_used].get(key)
                        if prev is None or score > prev[0]:
                            candidate[n_used][key] = (score, result)
            beams = {}
            for count, mapping in candidate.items():
                best = sorted(mapping.values(), key=lambda pair: (pair[0] +
                               (100.-pair[1].week_used) * .006), reverse=True)
                beams[count] = [s for _, s in best[:width]]
        pool = [s for group in beams.values() for s in group]
        chosen_state = max(pool, key=lambda s: s.served - .025*s.discarded - len(s.taken)*1.25)
        # Don't redeem to gain an immaterial sliver of estimated quota.
        if chosen_state.served < basestate.served + .6*len(chosen_state.taken):
            chosen_state = basestate
    marked = set(chosen_state.taken)
    state = init
    events = []
    by_day = {}
    old_w_end = init.week_end
    old_f_end = init.five_end
    for t, h, want in zip(grid, epoch_grid, demand):
        redeemed = h in marked
        prev_week_end, prev_five_end = state.week_end, state.five_end
        if redeemed:
            events.append({'ts': t, 'type': 'banked', 'label': 'Use saved reset'})
        state_next = advance(state, h, want, ratio, redeemed)
        got = state_next.served - state.served
        # The start of a fresh natural window is a forecast, not a known actual reset.
        if want > 0 and not redeemed:
            if prev_week_end and h >= prev_week_end:
                events.append({'ts': t, 'type': 'weekly', 'label': 'Weekly window refresh (est.)'})
            if prev_five_end and h >= prev_five_end:
                events.append({'ts': t, 'type': 'five', 'label': '5-hour window refresh (est.)'})
        day = t.astimezone().date()
        if day not in by_day:
            by_day[day] = {'date': day, 'demand': 0., 'served': 0., 'end_week_used': 0., 'end_five_used': 0.}
        by_day[day]['demand'] += want
        by_day[day]['served'] += got
        by_day[day]['end_week_used'] = state_next.week_used
        by_day[day]['end_five_used'] = state_next.five_used
        state = state_next
    chosen = [datetime.fromtimestamp(x, UTC) for x in chosen_state.taken]
    # Actual next announced reset times are facts observed in the log, not forecasts.
    if observation:
        for typ, stamp, label in (('weekly', observation.week_end, 'Reported weekly reset'),
                                  ('five', observation.five_end, 'Reported 5-hour reset')):
            if (stamp and stamp >= now and stamp <= grid[-1]
                    and not any(reset <= stamp for reset in chosen)):
                # A banked reset shifts the original schedule: never draw superseded clocks.
                events.append({'ts': stamp, 'type': typ, 'label': label, 'observed': True})
    events.sort(key=lambda e: (e['ts'], e['type']))
    gain = chosen_state.served - basestate.served
    if banked == 0:
        explanation = 'Enter your saved reset count to compare possible redemption dates.'
    elif not chosen:
        explanation = 'Hold your saved resets. In this scenario, redeeming one adds little forecast usable capacity.'
    else:
        explanation = (f'{len(chosen)} saved reset(s) recommended over {days} days; '
                       f'forecast improvement: {max(0.,gain):.1f} weekly-equivalent points.')
    return Plan(mode=mode, start=now, daily_target=daily, ratio=ratio,
                banked=banked, chosen=chosen, daily_rows=list(by_day.values()),
                events=events, demand_total=sum(demand), served_total=chosen_state.served,
                baseline_total=basestate.served, discarded=chosen_state.discarded,
                explanation=explanation)
