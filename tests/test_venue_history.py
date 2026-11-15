import json
import random
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src import venue_history as vh

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def moment(text):
    return vh.parse_time(text)


def assertion(assertion_id, revision, received_at, valid_from=None, valid_to=None,
              zone_type=None, store_key=None, operation="ASSERT", venue_key="v1"):
    return vh.Assertion(
        assertion_id=assertion_id, venue_key=venue_key, revision=revision,
        received_at=moment(received_at),
        valid_from=moment(valid_from) if valid_from else None,
        valid_to=moment(valid_to) if valid_to else None,
        zone_type=zone_type, store_key=store_key, operation=operation,
    )


def build(assertions, facts=()):
    history = vh.VenueHistory(assertions)
    return history, list(facts)


class SampleFixtureTests(unittest.TestCase):
    def setUp(self):
        self.history = vh.VenueHistory(vh.read_assertions(FIXTURES / "assertions.jsonl"))
        self.facts = vh.read_facts(FIXTURES / "facts.jsonl")

    def zone_at(self, as_of):
        _, rows = self.history.version(as_of)
        return {row.venue_key: (row.zone_type, row.store_key) for row in rows}

    def test_three_published_views(self):
        self.assertEqual(
            [vh.format_time(b) for b in self.history.known_batches()],
            ["2026-08-31T16:00:00+00:00", "2026-10-04T16:00:00+00:00", "2026-10-09T16:00:00+00:00"],
        )
        self.assertEqual(self.zone_at("2026-10-01T00:00:00+08:00"), {"v1": ("reading", "s1")})
        self.assertEqual(self.zone_at("2026-10-06T00:00:00+08:00"), {"v1": ("dining", "s2")})
        self.assertEqual(self.zone_at("2026-10-11T00:00:00+08:00"), {"v1": ("reading", "s1")})

    def test_effective_at_sample(self):
        early = self.history.effective_at("2026-09-25T10:00:00+08:00", as_of="2026-10-01T00:00:00+08:00")
        late = self.history.effective_at("2026-09-25T10:00:00+08:00", as_of="2026-10-06T00:00:00+08:00")
        self.assertEqual([(r["zone_type"], r["store_key"]) for r in early], [("reading", "s1")])
        self.assertEqual([(r["zone_type"], r["store_key"]) for r in late], [("dining", "s2")])

    def test_retraction_restores_latest_but_not_history(self):
        latest = self.history.effective_at("2026-09-25T10:00:00+08:00", as_of="2026-10-11T00:00:00+08:00")
        oct6 = self.history.effective_at("2026-09-25T10:00:00+08:00", as_of="2026-10-06T00:00:00+08:00")
        self.assertEqual([r["zone_type"] for r in latest], ["reading"])
        self.assertEqual([r["zone_type"] for r in oct6], ["dining"])

    def test_fact_remap_counts_and_unknown(self):
        before = self.history.remap(self.facts, as_of="2026-10-01T00:00:00+08:00")
        after = self.history.remap(self.facts, as_of="2026-10-06T00:00:00+08:00")
        self.assertEqual(len(before), len(self.facts))
        self.assertEqual(len(after), len(self.facts))
        self.assertEqual([r.zone_type for r in before], ["reading", None])
        self.assertEqual([r.zone_type for r in after], ["dining", None])
        unknown = [r for r in after if r.match_status == "unknown"]
        self.assertEqual([(r.fact_id, r.reason) for r in unknown], [("f2", "missing_venue")])

    def test_rebuild_is_idempotent(self):
        rebuilt = vh.VenueHistory(vh.read_assertions(FIXTURES / "assertions.jsonl"))
        for batch in self.history.known_batches():
            self.assertEqual(
                [r.as_row() for r in self.history.version(batch)[1]],
                [r.as_row() for r in rebuilt.version(batch)[1]],
            )
        first = self.history.remap(self.facts, as_of="2026-10-11T00:00:00+08:00")
        second = rebuilt.remap(self.facts, as_of="2026-10-11T00:00:00+08:00")
        self.assertEqual([r.as_row() for r in first], [r.as_row() for r in second])
        self.assertEqual(len(first), len(self.facts))


class BoundaryTests(unittest.TestCase):
    def test_half_open_boundaries(self):
        history = vh.VenueHistory([
            assertion("a", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", "2026-09-20T00:00:00+08:00", "reading", "s1"),
            assertion("b", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-20T00:00:00+08:00", None, "dining", "s2"),
        ])
        before = history.effective_at("2026-09-19T23:59:59+08:00", as_of="2026-09-02T00:00:00+08:00")
        at = history.effective_at("2026-09-20T00:00:00+08:00", as_of="2026-09-02T00:00:00+08:00")
        self.assertEqual([r["zone_type"] for r in before], ["reading"])
        self.assertEqual([r["zone_type"] for r in at], ["dining"])

    def test_timezone_normalised_across_offsets(self):
        history = vh.VenueHistory([
            assertion("a", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
        ])
        # 01:00Z 与 09:00+08:00 是同一时刻。
        hits = history.effective_at("2026-09-01T01:00:00+00:00", as_of="2026-09-01T00:00:00+08:00")
        self.assertEqual(len(hits), 1)

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            vh.parse_time("2026-09-01T00:00:00")

    def test_gap_between_assertions_is_unknown(self):
        history = vh.VenueHistory([
            assertion("a", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", "2026-09-10T00:00:00+08:00", "reading", "s1"),
            assertion("b", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-20T00:00:00+08:00", None, "dining", "s2"),
        ])
        fact = vh.Fact("f", "v1", moment("2026-09-15T00:00:00+08:00"), 100)
        [row] = history.remap([fact], as_of="2026-09-02T00:00:00+08:00")
        self.assertEqual((row.match_status, row.reason), ("unknown", "no_matching_interval"))


class OverlapTests(unittest.TestCase):
    def test_overlap_split_by_revision_then_assertion_id(self):
        history = vh.VenueHistory([
            assertion("a1", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
            assertion("a2", 2, "2026-09-02T00:00:00+08:00",
                      "2026-09-15T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "dining", "s2"),
        ])
        _, rows = history.version("2026-09-03T00:00:00+08:00")
        ranges = [(r.valid_from.isoformat(), vh.format_bound(r.valid_to), r.zone_type) for r in rows]
        self.assertEqual(ranges, [
            ("2026-08-31T16:00:00+00:00", "2026-09-14T16:00:00+00:00", "reading"),
            ("2026-09-14T16:00:00+00:00", "2026-09-30T16:00:00+00:00", "dining"),
            ("2026-09-30T16:00:00+00:00", "", "reading"),
        ])
        self.assertEqual(len(rows), 3)
        # 每个事实恰好命中一个片段，边界处归后一段。
        facts = [vh.Fact(f"f{i}", "v1", moment(t), 1) for i, t in enumerate([
            "2026-09-01T00:00:00+08:00", "2026-09-15T00:00:00+08:00", "2026-10-01T00:00:00+08:00",
        ])]
        rows_out = history.remap(facts, as_of="2026-09-03T00:00:00+08:00")
        self.assertEqual([r.zone_type for r in rows_out], ["reading", "dining", "reading"])

    def test_equal_revision_uses_larger_assertion_id(self):
        history = vh.VenueHistory([
            assertion("a9", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
            assertion("b1", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-10T00:00:00+08:00", None, "dining", "s2"),
        ])
        _, rows = history.version("2026-09-02T00:00:00+08:00")
        # b1 > a9 字典序，重叠段归 b1。
        self.assertEqual([(r.zone_type, r.source_assertion_id) for r in rows],
                         [("reading", "a9"), ("dining", "b1")])


class RevisionAndRetractionTests(unittest.TestCase):
    def test_higher_revision_replaces_within_assertion(self):
        assertions = [
            assertion("a", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
            assertion("a", 2, "2026-10-05T00:00:00+08:00",
                      "2026-09-20T00:00:00+08:00", None, "dining", "s2"),
        ]
        history = vh.VenueHistory(assertions)
        _, before = history.version("2026-10-01T00:00:00+08:00")
        _, after = history.version("2026-10-06T00:00:00+08:00")
        self.assertEqual([r.zone_type for r in before], ["reading"])
        self.assertEqual([r.zone_type for r in after], ["dining"])

    def test_retraction_of_replaced_declaration(self):
        assertions = [
            assertion("a1", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
            assertion("a2", 2, "2026-10-05T00:00:00+08:00",
                      "2026-09-20T00:00:00+08:00", None, "dining", "s2"),
            assertion("a2", 3, "2026-10-10T00:00:00+08:00", operation="RETRACT"),
        ]
        history = vh.VenueHistory(assertions)
        _, rows = history.version("2026-10-11T00:00:00+08:00")
        # 撤回只取消 a2 的当前声明，a1 未受影响，最新映射恢复 reading。
        self.assertEqual([r.zone_type for r in rows], ["reading"])
        # 已发布的历史视图保持不动：10 月 6 日 a2 的 dining 压过 a1，但仍保留 a1 的前段。
        _, older = history.version("2026-10-06T00:00:00+08:00")
        self.assertEqual([r.zone_type for r in older], ["reading", "dining"])

    def test_retract_before_any_assert_of_same_id(self):
        history = vh.VenueHistory([
            assertion("a", 2, "2026-10-05T00:00:00+08:00", operation="RETRACT"),
            assertion("a", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
        ])
        _, rows = history.version("2026-10-06T00:00:00+08:00")
        self.assertEqual(rows, [])
        fact = vh.Fact("f", "v1", moment("2026-09-25T10:00:00+08:00"), 1)
        [row] = history.remap([fact], as_of="2026-10-06T00:00:00+08:00")
        self.assertEqual((row.match_status, row.reason), ("unknown", "no_active_version"))

    def test_reassert_after_retract_restores_claim(self):
        history = vh.VenueHistory([
            assertion("a", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
            assertion("a", 2, "2026-10-05T00:00:00+08:00", operation="RETRACT"),
            assertion("a", 3, "2026-10-08T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
        ])
        self.assertEqual([r.zone_type for r in history.version("2026-10-06T00:00:00+08:00")[1]], [])
        self.assertEqual([r.zone_type for r in history.version("2026-10-09T00:00:00+08:00")[1]],
                         ["reading"])

    def test_lower_revision_arriving_later_is_ignored_within_batch(self):
        # 同一 received_at 是同一批次，高 revision 胜出，与文件顺序无关。
        for order in [(1, 2), (2, 1)]:
            records = [
                assertion("a", 1, "2026-10-05T00:00:00+08:00",
                          "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
                assertion("a", 2, "2026-10-05T00:00:00+08:00",
                          "2026-09-01T00:00:00+08:00", None, "dining", "s2"),
            ]
            history = vh.VenueHistory([records[i - 1] for i in order])
            _, rows = history.version("2026-10-06T00:00:00+08:00")
            self.assertEqual([r.zone_type for r in rows], ["dining"])


class OrderingAndIdempotenceTests(unittest.TestCase):
    ASSERTIONS = [
        assertion("a1", 1, "2026-09-01T00:00:00+08:00",
                  "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "reading", "s1"),
        assertion("a2", 2, "2026-10-05T00:00:00+08:00",
                  "2026-09-20T00:00:00+08:00", None, "dining", "s2"),
        assertion("a3", 1, "2026-10-05T00:00:00+08:00",
                  "2026-09-10T00:00:00+08:00", "2026-09-12T00:00:00+08:00", "event", "s3"),
        assertion("a2", 3, "2026-10-10T00:00:00+08:00", operation="RETRACT"),
    ]
    FACTS = [
        vh.Fact("f1", "v1", moment("2026-09-11T00:00:00+08:00"), 100),
        vh.Fact("f2", "v1", moment("2026-09-25T10:00:00+08:00"), 200),
        vh.Fact("f3", "v1", moment("2026-10-15T00:00:00+08:00"), 300),
        vh.Fact("f4", "nope", moment("2026-09-25T10:00:00+08:00"), 400),
    ]

    def snapshot(self, assertions):
        history = vh.VenueHistory(assertions)
        views = []
        for batch in history.known_batches():
            views.append((vh.format_time(batch), [r.as_row() for r in history.version(batch)[1]],
                          [r.as_row() for r in history.remap(self.FACTS, as_of=batch)]))
        return views

    def test_input_permutation_does_not_change_output(self):
        expected = self.snapshot(self.ASSERTIONS)
        shuffled = list(self.ASSERTIONS)
        for seed in range(12):
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(self.snapshot(shuffled), expected)

    def test_rebuild_keeps_surrogate_keys_and_fact_rows(self):
        first = self.snapshot(self.ASSERTIONS)
        second = self.snapshot(list(reversed(self.ASSERTIONS)))
        self.assertEqual(first, second)
        for _, versions, facts in first:
            self.assertEqual(len(facts), len(self.FACTS))
            self.assertEqual(len({r["surrogate_key"] for r in versions}), len(versions))

    def test_surrogate_key_ignores_tail_truncation(self):
        left = vh.surrogate_key("v1", moment("2026-09-20T00:00:00+08:00"), "dining", "s2")
        right = vh.surrogate_key("v1", moment("2026-09-19T16:00:00+00:00"), "dining", "s2")
        # 不同时区表示同一片段起点，代理键一致。
        self.assertEqual(left, right)

    def test_surrogate_key_changes_only_with_content(self):
        base = vh.surrogate_key("v1", moment("2026-09-20T00:00:00+08:00"), "dining", "s2")
        renamed = vh.surrogate_key("v1", moment("2026-09-20T00:00:00+08:00"), "dining", "s9")
        self.assertNotEqual(base, renamed)

    def test_truncating_assertion_keeps_prefix_key(self):
        history = vh.VenueHistory([
            assertion("a1", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
            assertion("a2", 1, "2026-10-05T00:00:00+08:00",
                      "2026-10-01T00:00:00+08:00", None, "dining", "s2"),
        ])
        facts = [vh.Fact("f", "v1", moment("2026-09-25T10:00:00+08:00"), 100)]
        [before] = history.remap(facts, as_of="2026-09-02T00:00:00+08:00")
        [after] = history.remap(facts, as_of="2026-10-06T00:00:00+08:00")
        # 九月事实在新批次里仍属 reading，且代理键未变。
        self.assertEqual((before.zone_type, after.zone_type), ("reading", "reading"))
        self.assertEqual(before.surrogate_key, after.surrogate_key)

    def test_query_returns_historical_views(self):
        history = vh.VenueHistory(self.ASSERTIONS)
        rows = history.timeline("2026-09-25T10:00:00+08:00")
        seen = [(r["point_in_time"], r["zone_type"]) for r in rows]
        # 第三批只撤回了 a2；a3 的 event 只覆盖 9/10–9/12，不覆盖查询时刻 9/25。
        self.assertEqual(seen, [
            ("2026-08-31T16:00:00+00:00", "reading"),
            ("2026-10-04T16:00:00+00:00", "dining"),
            ("2026-10-09T16:00:00+00:00", "reading"),
        ])

    def test_known_at_returns_full_snapshot(self):
        history = vh.VenueHistory(self.ASSERTIONS)
        rows = history.known_at("2026-10-06T00:00:00+08:00")
        self.assertEqual([r["zone_type"] for r in rows], ["reading", "event", "reading", "dining"])
        self.assertEqual({r["point_in_time"] for r in rows}, {"2026-10-04T16:00:00+00:00"})


class FuzzTests(unittest.TestCase):
    """随机声明流：不重复、不遗漏、边界唯一、重跑一致。"""

    def random_stream(self, rng):
        base = datetime(2026, 9, 1, tzinfo=vh._UTC)
        zones = ["reading", "dining", "event"]
        revisions: dict[str, int] = {}
        records = []
        for _ in range(rng.randint(3, 9)):
            start = base.fromtimestamp(
                base.timestamp() + rng.randint(0, 30) * 86400, tz=vh._UTC)
            length = rng.choice([3, 5, 7])
            assertion_id = f"a{rng.randint(1, 3)}"
            revisions[assertion_id] = revisions.get(assertion_id, 0) + 1
            records.append(assertion(
                assertion_id, revisions[assertion_id],
                f"2026-10-{rng.randint(1, 20):02d}T00:00:00+08:00",
                start.isoformat(), (start.fromtimestamp(
                    start.timestamp() + length * 86400, tz=vh._UTC)).isoformat(),
                rng.choice(zones), f"s{rng.randint(1, 2)}",
            ))
        return records

    def test_random_streams_are_consistent(self):
        rng = random.Random(20261115)
        for _ in range(40):
            records = self.random_stream(rng)
            facts = [
                vh.Fact(f"f{i}", "v1",
                        moment(f"2026-09-{rng.randint(1, 30):02d}T12:00:00+08:00"), 1)
                for i in range(6)
            ]
            history = vh.VenueHistory(records)
            shuffled = list(records)
            rng.shuffle(shuffled)
            twin = vh.VenueHistory(shuffled)
            for batch in history.known_batches():
                rows = history.version(batch)[1]
                # 片段互不重叠且按起点有序，每个事实至多命中一个。
                for left, right in zip(rows, rows[1:]):
                    self.assertLessEqual(left.end, right.valid_from)
                rows_out = history.remap(facts, as_of=batch)
                self.assertEqual(len(rows_out), len(facts))
                snapshot_keys = {r.surrogate_key for r in rows}
                # 匹配行只能引用本快照内的片段键，unknown 行不带键。
                for row in rows_out:
                    if row.match_status == "matched":
                        self.assertIn(row.surrogate_key, snapshot_keys)
                    else:
                        self.assertIsNone(row.surrogate_key)
            self.assertEqual(
                [r.as_row() for r in history.version(history.known_batches()[-1])[1]],
                [r.as_row() for r in twin.version(twin.known_batches()[-1])[1]],
            )


class ReaderTests(unittest.TestCase):
    def test_csv_assertions_supported(self):
        csv_text = (
            "assertion_id,venue_key,revision,received_at,valid_from,valid_to,zone_type,store_key,operation\n"
            "a1,v1,1,2026-09-01T00:00:00+08:00,2026-09-01T00:00:00+08:00,,reading,s1,ASSERT\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assertions.csv"
            path.write_text(csv_text, encoding="utf-8")
            [item] = vh.read_assertions(path)
        self.assertIsNone(item.valid_to)
        self.assertEqual(item.end, vh._FAR_FUTURE)

    def test_invalid_interval_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps([{
                "assertion_id": "a", "venue_key": "v1", "revision": 1,
                "received_at": "2026-09-01T00:00:00+08:00",
                "valid_from": "2026-09-10T00:00:00+08:00",
                "valid_to": "2026-09-10T00:00:00+08:00",
                "zone_type": "reading", "store_key": "s1",
            }]), encoding="utf-8")
            with self.assertRaises(ValueError):
                vh.read_assertions(path)

    def test_conflicting_same_revision_rejected(self):
        history = vh.VenueHistory([
            assertion("a", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", None, "reading", "s1"),
            assertion("a", 1, "2026-09-01T00:00:00+08:00",
                      "2026-09-01T00:00:00+08:00", None, "dining", "s2"),
        ])
        with self.assertRaises(ValueError):
            history.version("2026-09-02T00:00:00+08:00")


class CliTests(unittest.TestCase):
    def test_cli_reports_versions_facts_and_queries(self):
        out = Path(tempfile.mkdtemp()) / "nested" / "report.json"
        unknown = out.parent / "unknown.jsonl"
        code = vh.main([
            "--assertions", str(FIXTURES / "assertions.jsonl"),
            "--facts", str(FIXTURES / "facts.jsonl"),
            "--as-of", "2026-10-06T00:00:00+08:00",
            "--query", "timeline@2026-09-25T10:00:00+08:00",
            "--query", "effective_at@2026-09-25T10:00:00+08:00@2026-10-01T00:00:00+08:00",
            "--unknown-report", str(unknown),
            "--out", str(out),
        ])
        self.assertEqual(code, 0)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["fact_count"], 2)
        self.assertEqual(report["unknown_count"], 1)
        self.assertEqual([r["zone_type"] for r in report["remaps"]], ["dining", None])
        self.assertEqual([r["zone_type"] for r in report["queries"][1]["results"]], ["reading"])
        self.assertEqual([r["zone_type"] for r in report["queries"][0]["results"]],
                         ["reading", "dining", "reading"])
        self.assertEqual(len(unknown.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
