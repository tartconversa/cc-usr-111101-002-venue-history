"""可复现示例：边界、撤回、乱序。

运行：python -m src.examples
输出固定顺序的 JSON，可直接用于对比两次运行（幂等）。
"""

from __future__ import annotations

import json

from .venue_history import (
    Assertion, Fact, VenueHistory, format_bound, format_time, parse_time,
)


def a(assertion_id, revision, received_at, valid_from=None, valid_to=None,
      zone_type=None, store_key=None, operation="ASSERT", venue_key="v1"):
    return Assertion(
        assertion_id=assertion_id, venue_key=venue_key, revision=revision,
        received_at=parse_time(received_at),
        valid_from=parse_time(valid_from) if valid_from else None,
        valid_to=parse_time(valid_to) if valid_to else None,
        zone_type=zone_type, store_key=store_key, operation=operation,
    )


def f(fact_id, when, amount=100, venue_key="v1"):
    return Fact(fact_id, venue_key, parse_time(when), amount)


def describe(history, facts):
    out = {"published_views": [], "fact_rows": [], "unknown": []}
    for batch in history.known_batches():
        _, rows = history.version(batch)
        out["published_views"].append({
            "known_at": format_time(batch),
            "slices": [
                {
                    "valid_from": format_time(r.valid_from),
                    "valid_to": format_bound(r.valid_to),
                    "zone_type": r.zone_type,
                    "store_key": r.store_key,
                    "surrogate_key": r.surrogate_key,
                    "source": f"{r.source_assertion_id}@r{r.source_revision}",
                }
                for r in rows
            ],
        })
        rows_out = history.remap(facts, as_of=batch)
        out["fact_rows"].append({
            "known_at": format_time(batch),
            "count": len(rows_out),
            "rows": [
                {"fact_id": r.fact_id, "zone_type": r.zone_type,
                 "surrogate_key": r.surrogate_key, "match_status": r.match_status}
                for r in rows_out
            ],
        })
        out["unknown"].extend(
            {"known_at": format_time(batch), "fact_id": r.fact_id, "reason": r.reason}
            for r in rows_out if r.match_status == "unknown"
        )
    return out


SCENARIOS = {
    # 边界：三条声明首尾相接，验证左闭右开与切分数量。
    "boundary": (
        [
            a("a1", 1, "2026-09-01T00:00:00+08:00",
              "2026-09-01T00:00:00+08:00", "2026-09-15T00:00:00+08:00", "reading", "s1"),
            a("a2", 1, "2026-09-01T00:00:00+08:00",
              "2026-09-15T00:00:00+08:00", "2026-09-25T00:00:00+08:00", "dining", "s2"),
            a("a3", 1, "2026-09-01T00:00:00+08:00",
              "2026-09-25T00:00:00+08:00", None, "event", "s3"),
        ],
        [
            f("before", "2026-09-14T23:59:59+08:00"),
            f("on_boundary", "2026-09-15T00:00:00+08:00"),
            f("end_of_second", "2026-09-24T23:59:59+08:00"),
            f("open_ended", "2027-01-01T00:00:00+08:00"),
        ],
    ),
    # 重叠：更高 revision 切掉中间一段，两端仍属旧声明。
    "overlap": (
        [
            a("a1", 1, "2026-09-01T00:00:00+08:00",
              "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
            a("a2", 2, "2026-10-05T00:00:00+08:00",
              "2026-09-15T00:00:00+08:00", "2026-09-25T00:00:00+08:00", "dining", "s2"),
        ],
        [
            f("left", "2026-09-10T00:00:00+08:00"),
            f("middle", "2026-09-20T00:00:00+08:00"),
            f("right", "2026-09-30T00:00:00+08:00"),
        ],
    ),
    # 撤回：三个批次，最新视图恢复，历史视图冻结。
    "retraction": (
        [
            a("a1", 1, "2026-09-01T00:00:00+08:00",
              "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
            a("a2", 2, "2026-10-05T00:00:00+08:00",
              "2026-09-20T00:00:00+08:00", None, "dining", "s2"),
            a("a2", 3, "2026-10-10T00:00:00+08:00", operation="RETRACT"),
        ],
        [f("september", "2026-09-25T10:00:00+08:00")],
    ),
    # 乱序：文件顺序与 received_at 顺序相反，且高 revision 先出现在文件里。
    # 批次只由 received_at 决定，因此视图、代理键、事实行数都不随排列变化。
    "out_of_order": (
        [
            a("a2", 2, "2026-10-05T00:00:00+08:00",
              "2026-09-20T00:00:00+08:00", None, "dining", "s2"),
            a("a3", 1, "2026-09-15T00:00:00+08:00",
              "2026-09-10T00:00:00+08:00", "2026-09-12T00:00:00+08:00", "event", "s3"),
            a("a1", 1, "2026-09-01T00:00:00+08:00",
              "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
        ],
        [
            f("september", "2026-09-25T10:00:00+08:00"),
            f("event_day", "2026-09-11T00:00:00+08:00"),
            f("orphan", "2026-09-25T10:00:00+08:00", venue_key="missing"),
        ],
    ),
}


def main():
    report = {}
    for name, (assertions, facts) in SCENARIOS.items():
        history = VenueHistory(assertions)
        report[name] = describe(history, facts)
        # 逆序输入必须得到完全相同的报告。
        reversed_report = describe(VenueHistory(list(reversed(assertions))), facts)
        report[name]["order_independent"] = reversed_report == report[name]

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
