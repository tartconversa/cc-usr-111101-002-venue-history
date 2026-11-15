"""双时间场地维表：声明整理、有效片段切分、事实重映射。

时间语义
--------
* 业务时间：``valid_from`` / ``valid_to``，区间左闭右开，空 ``valid_to`` 为无穷远。
* 获知时间：``received_at``，同一 ``received_at`` 视为一个发布批次。
* 所有时刻必须带时区，统一换算为 UTC 后比较。

声明整理
--------
* 同一 ``assertion_id`` 内 ``revision`` 高者替代低者；``RETRACT`` 取消该声明。
* 不同声明在同一业务时间点重叠时，按 ``(revision, assertion_id)`` 取较大者，
  切分出互不重叠、覆盖并集的有效片段。没有声明覆盖的空洞不生成片段。
* 代理键由 ``venue_key``、片段起点与片段内容（分区/门店）派生，不含
  ``valid_to`` 和获知批次，因此输入顺序、撤回、仅截短结尾的更正都不会改动
  已有事实的关联；``known_at`` 区分同一代理键在不同发布批次的版本。

发布与查询
----------
``VenueHistory.version(as_of)`` 只使用 ``received_at <= as_of`` 的声明构建当时的
维表视图；已发布的视图是不可变快照，之后的撤回或更正不会改写它。
事实在 ``effective_at`` 上匹配唯一片段；没有匹配时保留原值并标记 ``unknown``。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .assertion_reader import load

_UTC = timezone.utc
_FAR_FUTURE = datetime.max.replace(tzinfo=_UTC)


def parse_time(value: str) -> datetime:
    """解析带时区 ISO-8601 时间，统一换算为 UTC；无时区直接拒绝。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间缺少时区: {value}")
    return parsed.astimezone(_UTC)


def format_time(value: datetime) -> str:
    return value.astimezone(_UTC).isoformat()


def format_bound(value: datetime | None) -> str:
    return "" if value is None or value >= _FAR_FUTURE else format_time(value)


@dataclass(frozen=True)
class Assertion:
    assertion_id: str
    venue_key: str
    revision: int
    received_at: datetime
    valid_from: datetime | None
    valid_to: datetime | None
    zone_type: str | None
    store_key: str | None
    operation: str = "ASSERT"

    @property
    def end(self) -> datetime:
        return self.valid_to if self.valid_to is not None else _FAR_FUTURE


@dataclass(frozen=True)
class Segment:
    venue_key: str
    valid_from: datetime
    valid_to: datetime | None
    zone_type: str | None
    store_key: str | None
    source_assertion_id: str
    source_revision: int

    @property
    def end(self) -> datetime:
        return self.valid_to if self.valid_to is not None else _FAR_FUTURE

    def contains(self, moment: datetime) -> bool:
        return self.valid_from <= moment < self.end


@dataclass(frozen=True)
class ZoneVersion:
    """维表行：一个业务有效片段在某个获知批次下的可见版本。"""

    venue_key: str
    surrogate_key: str
    valid_from: datetime
    valid_to: datetime | None
    zone_type: str | None
    store_key: str | None
    known_at: datetime
    source_assertion_id: str | None
    source_revision: int | None

    @property
    def end(self) -> datetime:
        return self.valid_to if self.valid_to is not None else _FAR_FUTURE

    def contains(self, moment: datetime) -> bool:
        return self.valid_from <= moment < self.end

    def as_row(self) -> dict:
        return {
            "venue_key": self.venue_key,
            "surrogate_key": self.surrogate_key,
            "valid_from": format_time(self.valid_from),
            "valid_to": format_bound(self.valid_to),
            "zone_type": self.zone_type,
            "store_key": self.store_key,
            "known_at": format_time(self.known_at),
            "source_assertion_id": self.source_assertion_id,
            "source_revision": self.source_revision,
        }


@dataclass(frozen=True)
class Fact:
    fact_id: str
    venue_key: str
    effective_at: datetime
    amount_fen: int


@dataclass(frozen=True)
class FactRow:
    fact_id: str
    venue_key: str
    effective_at: datetime
    amount_fen: int
    surrogate_key: str | None
    zone_type: str | None
    store_key: str | None
    match_status: str
    reason: str | None

    def as_row(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "venue_key": self.venue_key,
            "effective_at": format_time(self.effective_at),
            "amount_fen": self.amount_fen,
            "surrogate_key": self.surrogate_key,
            "zone_type": self.zone_type,
            "store_key": self.store_key,
            "match_status": self.match_status,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# 输入解析
# ---------------------------------------------------------------------------

def read_assertions(path) -> list[Assertion]:
    raw = load(path)
    if isinstance(raw, bytes):
        raise ValueError("声明输入必须是 jsonl、csv 或 json")
    if isinstance(raw, dict):
        raw = raw.get("assertions") or raw.get("items") or [raw]

    assertions = []
    for position, item in enumerate(raw):
        operation = str(item.get("operation", "ASSERT")).upper()
        if operation not in {"ASSERT", "RETRACT"}:
            raise ValueError(f"第 {position} 条声明 operation 非法: {operation}")
        valid_from = parse_time(item["valid_from"]) if item.get("valid_from") else None
        valid_to = parse_time(item["valid_to"]) if item.get("valid_to") else None
        if operation == "ASSERT":
            if valid_from is None:
                raise ValueError(f"第 {position} 条声明缺少 valid_from")
            if valid_to is not None and valid_to <= valid_from:
                raise ValueError(f"第 {position} 条声明 valid_to 不晚于 valid_from")
        assertions.append(
            Assertion(
                assertion_id=str(item["assertion_id"]),
                venue_key=str(item["venue_key"]),
                revision=int(item["revision"]),
                received_at=parse_time(item["received_at"]),
                valid_from=valid_from,
                valid_to=valid_to,
                zone_type=item.get("zone_type"),
                store_key=item.get("store_key"),
                operation=operation,
            )
        )
    return assertions


def read_facts(path) -> list[Fact]:
    raw = load(path)
    if isinstance(raw, dict):
        raw = raw.get("facts") or raw.get("items") or [raw]
    return [
        Fact(
            fact_id=str(item["fact_id"]),
            venue_key=str(item["venue_key"]),
            effective_at=parse_time(item["effective_at"]),
            amount_fen=int(item["amount_fen"]),
        )
        for item in raw
    ]


# ---------------------------------------------------------------------------
# 声明整理与切分
# ---------------------------------------------------------------------------

def _signature(item: Assertion):
    return (item.operation, item.venue_key, item.valid_from, item.valid_to,
            item.zone_type, item.store_key)


def _latest_by_assertion(assertions: list[Assertion], *, as_of: datetime) -> dict[str, Assertion]:
    """每个 assertion_id 在 as_of 时刻已知的最高 revision 记录。"""
    latest: dict[str, Assertion] = {}
    for item in assertions:
        if item.received_at > as_of:
            continue
        current = latest.get(item.assertion_id)
        if current is None or (item.revision, item.received_at) > (current.revision, current.received_at):
            latest[item.assertion_id] = item
        elif (item.revision, item.received_at) == (current.revision, current.received_at):
            # 同一声明同一修订同一批次却内容不同：输入自相矛盾，拒绝而不是依赖排列。
            if _signature(item) != _signature(current):
                raise ValueError(
                    f"assertion {item.assertion_id} revision {item.revision} 在同一批次 "
                    f"{format_time(item.received_at)} 存在冲突内容"
                )
    return latest


def _active_assertions(assertions: list[Assertion], *, as_of: datetime) -> list[Assertion]:
    active = [
        item
        for item in _latest_by_assertion(assertions, as_of=as_of).values()
        if item.operation == "ASSERT"
    ]
    # 同一 received_at 是发布批次：排序完全确定，输入排列不影响结果。
    active.sort(key=lambda a: (a.venue_key, a.valid_from, a.end, a.revision, a.assertion_id))
    return active


def _priority(item: Assertion) -> tuple[int, str]:
    return (item.revision, item.assertion_id)


def split_segments(assertions: list[Assertion]) -> list[Segment]:
    """把重叠声明切成互不重叠的有效片段，边界处优先级高者获胜。"""
    by_venue: dict[str, list[Assertion]] = {}
    for item in assertions:
        by_venue.setdefault(item.venue_key, []).append(item)

    segments: list[Segment] = []
    for venue_key in sorted(by_venue):
        statements = by_venue[venue_key]
        boundaries = sorted({a.valid_from for a in statements} | {a.end for a in statements})
        for start, end in zip(boundaries, boundaries[1:]):
            if end <= start:
                continue
            # 左闭右开：片段 [start, end) 内归属恒定，用 start 判定即可。
            covering = [a for a in statements if a.valid_from <= start < a.end]
            if not covering:
                continue
            winner = max(covering, key=_priority)
            candidate = Segment(
                venue_key=venue_key,
                valid_from=start,
                valid_to=None if end >= _FAR_FUTURE else end,
                zone_type=winner.zone_type,
                store_key=winner.store_key,
                source_assertion_id=winner.assertion_id,
                source_revision=winner.revision,
            )
            if segments and _mergeable(segments[-1], candidate):
                previous = segments.pop()
                segments.append(replace(previous, valid_to=candidate.valid_to))
            else:
                segments.append(candidate)
    segments.sort(key=lambda s: (s.venue_key, s.valid_from, s.end))
    return segments


def _mergeable(left: Segment, right: Segment) -> bool:
    """相邻且属性、胜出来源都相同的片段无需在中间切开。"""
    return (
        left.venue_key == right.venue_key
        and left.valid_from < right.valid_from
        and left.end == right.valid_from
        and (left.zone_type, left.store_key, left.source_assertion_id, left.source_revision)
        == (right.zone_type, right.store_key, right.source_assertion_id, right.source_revision)
    )


def surrogate_key(venue_key: str, valid_from: datetime, zone_type: str | None,
                  store_key: str | None) -> str:
    """代理键：业务片段的起点 + 片段内容。

    不含 ``valid_to`` 与获知批次，因此后续声明只是把某段的结尾截短时，
    该段起点之前的销售仍落在同一个代理键上，不因修订重新分桶；内容
    （分区/门店）真正改变时才产生新键。同一快照内片段起点互不相同，
    所以 ``(venue_key, valid_from)`` 已能唯一定位一行。``known_at`` 用来
    区分同一代理键在不同发布批次的版本。
    """
    return "|".join(
        (venue_key, format_time(valid_from), zone_type or "", store_key or "")
    )


# ---------------------------------------------------------------------------
# 双时间维表
# ---------------------------------------------------------------------------

class VenueHistory:
    def __init__(self, assertions: list[Assertion]):
        self.assertions = list(assertions)
        self._known_venues = {a.venue_key for a in self.assertions}
        self._segments: dict[datetime, tuple[Segment, ...]] = {}
        self._versions: dict[datetime, tuple[ZoneVersion, ...]] = {}

    def known_batches(self) -> list[datetime]:
        return sorted({a.received_at for a in self.assertions})

    def _resolve(self, as_of: datetime) -> tuple[datetime, tuple[Segment, ...]]:
        """返回 (生效的发布批次, 该批次切出的片段)。as_of 落在批次之间时取之前的最近批次。"""
        batches = [b for b in self.known_batches() if b <= as_of]
        if not batches:
            return as_of, ()
        known_at = batches[-1]
        if known_at not in self._segments:
            self._segments[known_at] = tuple(
                split_segments(_active_assertions(self.assertions, as_of=known_at))
            )
        return known_at, self._segments[known_at]

    def version(self, as_of) -> tuple[datetime, list[ZoneVersion]]:
        """as_of 时刻可见的维表快照。"""
        as_of = parse_time(as_of) if isinstance(as_of, str) else as_of
        known_at, segments = self._resolve(as_of)
        if known_at not in self._versions:
            self._versions[known_at] = tuple(
                ZoneVersion(
                    venue_key=s.venue_key,
                    surrogate_key=surrogate_key(
                        s.venue_key, s.valid_from, s.zone_type, s.store_key
                    ),
                    valid_from=s.valid_from,
                    valid_to=s.valid_to,
                    zone_type=s.zone_type,
                    store_key=s.store_key,
                    known_at=known_at,
                    source_assertion_id=s.source_assertion_id,
                    source_revision=s.source_revision,
                )
                for s in segments
            )
        return known_at, list(self._versions[known_at])

    # 查询接口 ------------------------------------------------------------
    def effective_at(self, moment, *, as_of=None) -> list[dict]:
        """业务时间轴：as_of 时点已知、覆盖 moment 的场地状态。"""
        moment = parse_time(moment) if isinstance(moment, str) else moment
        known_at, rows = self.version(as_of if as_of is not None else self.known_batches()[-1])
        hits = [r for r in rows if r.contains(moment)]
        return [
            {**r.as_row(), "query": "effective_at", "query_time": format_time(moment)}
            for r in hits
        ]

    def known_at(self, as_of) -> list[dict]:
        """获知时间轴：as_of 时点可见的完整维表快照。"""
        known_at, rows = self.version(as_of)
        return [
            {**r.as_row(), "query": "known_at", "point_in_time": format_time(known_at)}
            for r in rows
        ]

    def timeline(self, moment, *, venue_key=None) -> list[dict]:
        """同一业务时刻在各发布批次下的认知变化，按 known_at 排序。"""
        moment = parse_time(moment) if isinstance(moment, str) else moment
        rows = []
        for batch in self.known_batches():
            _, snapshot = self.version(batch)
            rows.extend(
                {**r.as_row(), "query": "timeline", "query_time": format_time(moment),
                 "point_in_time": format_time(batch)}
                for r in snapshot
                if r.contains(moment) and (venue_key is None or r.venue_key == venue_key)
            )
        return rows

    def history(self, venue_key: str | None = None) -> list[dict]:
        """所有发布批次的维表快照，按 known_at、valid_from 排序。"""
        rows = []
        for batch in self.known_batches():
            _, snapshot = self.version(batch)
            for row in snapshot:
                if venue_key is not None and row.venue_key != venue_key:
                    continue
                rows.append({**row.as_row(), "point_in_time": format_time(batch)})
        return rows

    # 事实重映射 ----------------------------------------------------------
    def remap(self, facts: list[Fact], *, as_of) -> list[FactRow]:
        known_at, rows = self.version(as_of)
        by_venue: dict[str, list[ZoneVersion]] = {}
        for row in rows:
            by_venue.setdefault(row.venue_key, []).append(row)
        result = []
        for fact in facts:
            candidates = [r for r in by_venue.get(fact.venue_key, []) if r.contains(fact.effective_at)]
            if len(candidates) > 1:  # 维表保证不会发生，显式防御。
                raise AssertionError(f"事实 {fact.fact_id} 命中多个维表版本")
            if not candidates:
                if fact.venue_key not in by_venue:
                    # 声明里从未出现过该场地 → 无法归属；出现过但本快照无有效片段 → 当时未知。
                    reason = "missing_venue" if fact.venue_key not in self._known_venues else "no_active_version"
                else:
                    reason = "no_matching_interval"
                result.append(FactRow(
                    fact_id=fact.fact_id, venue_key=fact.venue_key,
                    effective_at=fact.effective_at, amount_fen=fact.amount_fen,
                    surrogate_key=None, zone_type=None, store_key=None,
                    match_status="unknown", reason=reason,
                ))
                continue
            row = candidates[0]
            result.append(FactRow(
                fact_id=fact.fact_id, venue_key=fact.venue_key,
                effective_at=fact.effective_at, amount_fen=fact.amount_fen,
                surrogate_key=row.surrogate_key, zone_type=row.zone_type,
                store_key=row.store_key, match_status="matched", reason=None,
            ))
        return result


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _write(value, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".jsonl":
        rows = value if isinstance(value, list) else [value]
        with path.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    else:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")


def parse_query(text: str) -> tuple[str, dict]:
    """``effective_at@<业务时间>[@<获知时间>]``、``known_at@<获知时间>``、``timeline@<业务时间>``。"""
    parts = text.split("@")
    if parts[0] == "effective_at" and len(parts) in {2, 3}:
        return "effective_at", {"moment": parts[1], "as_of": parts[2] if len(parts) == 3 else None}
    if parts[0] == "known_at" and len(parts) == 2:
        return "known_at", {"as_of": parts[1]}
    if parts[0] == "timeline" and len(parts) == 2:
        return "timeline", {"moment": parts[1]}
    raise ValueError(f"无法解析查询: {text}")


def run_query(history: "VenueHistory", text: str) -> list[dict]:
    mode, payload = parse_query(text)
    if mode == "effective_at":
        return history.effective_at(payload["moment"], as_of=payload["as_of"])
    if mode == "known_at":
        return history.known_at(payload["as_of"])
    return history.timeline(payload["moment"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="双时间场地维表构建、查询与事实重映射")
    parser.add_argument("--assertions", default="fixtures/assertions.jsonl")
    parser.add_argument("--facts", default="fixtures/facts.jsonl")
    parser.add_argument("--as-of", action="append", default=None, help="获知时间，可重复；默认全部批次")
    parser.add_argument("--query", action="append", default=[],
                        help="effective_at@<业务时间>[@<获知时间>]、known_at@<获知时间>、timeline@<业务时间>")
    parser.add_argument("--unknown-report", help="unknown 事实明细输出路径")
    parser.add_argument("--out", help="完整汇总输出路径")
    args = parser.parse_args(argv)

    history = VenueHistory(read_assertions(args.assertions))
    facts = read_facts(args.facts)
    batches = history.known_batches()
    as_ofs = [parse_time(value) for value in args.as_of] if args.as_of else batches

    versions, remaps, unknown = [], [], []
    for as_of in as_ofs:
        _, rows = history.version(as_of)
        versions.extend({**row.as_row(), "point_in_time": format_time(as_of)} for row in rows)
        for row in history.remap(facts, as_of=as_of):
            remaps.append({**row.as_row(), "point_in_time": format_time(as_of)})
            if row.match_status == "unknown":
                unknown.append({**row.as_row(), "point_in_time": format_time(as_of)})

    queries = [{"query": text, "results": run_query(history, text)} for text in args.query]

    report = {
        "known_batches": [format_time(b) for b in batches],
        "versions": versions,
        "remaps": remaps,
        "queries": queries,
        "fact_count": len(facts) * len(as_ofs),
        "unknown_count": len(unknown),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))

    if args.unknown_report:
        _write(unknown, Path(args.unknown_report))
    if args.out:
        _write(report, Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
