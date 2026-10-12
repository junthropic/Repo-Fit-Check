"""Offline regression tests for the four scout problems. Run: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import discover as D  # noqa: E402
import scan_env as S  # noqa: E402

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def repo(name, stars=0, desc="", topics=(), lic="MIT", pushed="2026-10-01T00:00:00Z", archived=False, fork=False):
    return {"full_name": name, "description": desc, "stars": stars, "pushed_at": pushed, "license": lic,
            "archived": archived, "fork": fork, "language": None, "topics": list(topics), "url": "", "open_issues": 0}


class FakeGH:
    """Maps a substring of the query to results; records every query."""

    def __init__(self, table):
        self.table, self.queries = table, []

    def search(self, q, per_page=30, sort=None):
        self.queries.append(q)
        for key, res in self.table.items():
            if key in q:
                return res
        return []


class StarCache(unittest.TestCase):
    def test_default_cache_is_under_the_user_home(self):
        self.assertTrue(D.GH().cache_dir.is_relative_to(Path.home()))

    def test_cache_dir_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(D.GH(cache_dir=d).cache_dir, Path(d))


class Problem1_NoGitHubMcp(unittest.TestCase):
    def test_backend_is_gh_cli_only(self):
        src = (Path(D.__file__)).read_text(encoding="utf-8")
        self.assertNotIn("mcp__github", src)
        self.assertIn('["gh"]', src)


class Problem2_EnvScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        c = self.home / ".claude"
        (c / "skills" / "mine").mkdir(parents=True)
        (c / "skills" / "mine" / "SKILL.md").write_text("---\nname: mine\ndescription: my own skill\n---\n", encoding="utf-8")
        pl = c / "plugins" / "synced" / "bucket" / "buildkite~mkt"
        (pl / "skills" / "buildkite-pipelines").mkdir(parents=True)
        (pl / "skills" / "buildkite-pipelines" / "SKILL.md").write_text(
            "---\nname: buildkite-pipelines\ndescription: Build CI pipelines\n---\n", encoding="utf-8")
        (pl / ".mcp.json").write_text(json.dumps({"mcpServers": {"bk": {"url": "https://x", "headers": {"Authorization": "Bearer SECRET-TOKEN-123"}}}}), encoding="utf-8")
        (self.home / ".claude.json").write_text(json.dumps({
            "mcpServers": {"rea": {"command": "x", "env": {"API_KEY": "SECRET-TOKEN-123"}}},
            "claudeAiMcpEverConnected": ["claude.ai Notion"]}), encoding="utf-8")
        # a skill whose long description merely mentions a keyword once: must not count as coverage
        (c / "skills" / "stray").mkdir()
        (c / "skills" / "stray" / "SKILL.md").write_text(
            "---\nname: stray\ndescription: Sales helper. Also mentions deploy once in passing.\n---\n", encoding="utf-8")
        (c / "settings.json").write_text(json.dumps({"enabledPlugins": {"off@x": False}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sees_plugin_skills_mcp_and_connectors(self):
        r = S.scan(self.home)
        names = {s["name"] for s in r["skills"]}
        self.assertIn("buildkite-pipelines", names)  # plugin skill, outside ~/.claude/skills
        self.assertIn("mine", names)
        mcp = {(m["name"], m["source"]) for m in r["mcp"]}
        self.assertIn(("buildkite:bk", "plugin-bundled"), mcp)
        self.assertIn(("rea", "user-config"), mcp)
        self.assertIn(("claude.ai Notion", "claude.ai-connector(ever-connected)"), mcp)

    def test_never_emits_secret_values(self):
        out = json.dumps(S.scan(self.home), ensure_ascii=False) + S.summary(S.scan(self.home))
        self.assertNotIn("SECRET-TOKEN-123", out)

    def test_stray_description_word_is_not_coverage(self):
        r = S.scan(self.home)
        cicd = next(c for c in r["coverage"]["covered"] if c["id"] == "cicd")
        self.assertTrue(all("buildkite" in e for e in cicd["evidence"]))
        self.assertNotIn("skill:stray", cicd["evidence"])
        self.assertEqual(cicd["confidence"], "strong")  # skill + plugin + mcp all carry the name
        self.assertTrue(any(g["id"] == "database" for g in r["coverage"]["gaps"]))

    def test_uncovered_category_stays_a_gap(self):
        r = S.scan(self.home)
        git = [c for c in r["coverage"]["covered"] if c["id"] == "orchestration"]
        self.assertFalse(git)  # nothing in the fixture mentions it: a gap, not coverage

    def test_weak_and_gap_entries_both_carry_search_terms(self):
        r = S.scan(self.home)
        for entry in r["coverage"]["gaps"] + r["coverage"]["covered"]:
            self.assertIn("search", entry)

    def test_disabled_plugin_does_not_cover(self):
        r = S.scan(self.home)
        self.assertIn("off", r["counts"]["plugins"]["enabled_false"])

    def test_live_names_are_merged(self):
        live = self.home / "live.txt"
        live.write_text("mcp:Supabase\nskill:postgres-tuning\n", encoding="utf-8")
        r = S.scan(self.home, live_path=str(live))
        db = next(c for c in r["coverage"]["covered"] if c["id"] == "database")
        self.assertTrue(any("postgres" in e.lower() for e in db["evidence"]))


class Problem3_StarSkew(unittest.TestCase):
    groups = [["labor", "employment"], ["law", "statute"], ["compliance"]]

    def test_small_relevant_beats_huge_irrelevant(self):
        repos = [repo("big/cloud-scanner", 40000, "cloud security posture compliance", ["compliance", "security"]),
                 repo("tiny/labor-law-checker", 30, "labor law compliance checker", ["labor-law", "compliance"])]
        scored, _ = D.rank(repos, self.groups, now=NOW)
        self.assertEqual(scored[0]["full_name"], "tiny/labor-law-checker")

    def test_stars_are_log_scaled_and_capped(self):
        a = D.score_repo(repo("a/x", 100_000, "labor law compliance"), self.groups, now=NOW)
        b = D.score_repo(repo("b/x", 1_000_000, "labor law compliance"), self.groups, now=NOW)
        self.assertEqual(a["pop"], b["pop"])  # capped at 1.0

    def test_small_picks_not_crowded_out(self):
        big = [repo(f"big/r{i}", 50000, "labor law compliance engine", ["labor", "law", "compliance"]) for i in range(12)]
        small = [repo("small/gem", 12, "labor law compliance", ["labor", "law"])]
        scored, _ = D.rank(big + small, self.groups, now=NOW)
        main, picks = D.select(scored, top=10)
        self.assertIn("small/gem", {r["full_name"] for r in main + picks})

    def test_rare_terms_weigh_more(self):
        pool = [repo(f"x/p{i}", 5, "generic compliance tool", ["compliance"]) for i in range(30)]
        pool.append(repo("y/korea-labor", 5, "labor tool", ["labor"]))
        w = D.idf_weights(pool, self.groups)
        self.assertGreater(w[0], w[2])  # 'labor' is rarer than 'compliance'

    def test_filters(self):
        repos = [repo("a/archived", archived=True, desc="labor law compliance"),
                 repo("b/forked", fork=True, desc="labor law compliance"),
                 repo("c/agpl", lic="AGPL-3.0", desc="labor law compliance"),
                 repo("d/ok", lic="MIT", desc="labor law compliance"),
                 repo("e/gpl-mismatch", lic="GPL-3.0", desc="labor law compliance")]
        scored, excluded = D.rank(repos, self.groups, allowed=["MIT"], avoid=["AGPL"], now=NOW)
        names = [r["full_name"] for r in scored]
        self.assertEqual(names[0], "d/ok")
        self.assertNotIn("c/agpl", names)
        self.assertEqual({n for n, _ in excluded}, {"a/archived", "b/forked", "c/agpl"})
        self.assertEqual(next(r for r in scored if r["full_name"] == "e/gpl-mismatch")["lic_status"], "mismatch")


class Problem4_EmptyKeywordMode(unittest.TestCase):
    def test_korean_purpose_becomes_english_groups(self):
        g = D.parse_terms(purpose="한국 노동 법령 준수 점검 플랫폼")
        flat = {w for grp in g for w in grp}
        self.assertTrue({"korea", "labor", "law", "compliance"} <= flat)
        self.assertNotIn("platform", flat)  # generic word dropped

    def test_law_suffix_in_a_compound_word_gives_a_law_term(self):
        g = D.parse_terms(purpose="한국 노동법 준수를 점검하는 도구 찾고 있어")
        flat = {w for grp in g for w in grp}
        self.assertTrue({"korea", "labor", "law", "compliance"} <= flat)

    def test_words_that_only_contain_the_character_do_not_trigger_law(self):
        for text, expected in [("문서 작성 방법", "document"), ("pdf 사용법", "pdf"), ("법인 회계", "accounting"),
                               ("영어 문법 pdf", "pdf")]:
            flat = {w for grp in D.parse_terms(purpose=text) for w in grp}
            self.assertIn(expected, flat, text)
            self.assertFalse({"law", "statute"} & flat, f"{text!r} should not mean law: {flat}")

    def test_words_that_mean_something_else_map_to_their_own_terms(self):
        flat = {w for grp in D.parse_terms(purpose="법인 회계") for w in grp}
        self.assertIn("corporation", flat)
        flat = {w for grp in D.parse_terms(purpose="불법 scraper") for w in grp}
        self.assertIn("illegal", flat)

    def test_an_ignored_word_alone_is_not_a_usable_query(self):
        with self.assertRaises(ValueError):
            D.parse_terms(purpose="방법")

    def test_empty_glossary_value_means_ignore_not_crash(self):
        g = D.parse_terms(purpose="방법 pdf", glossary={"방법": ""})
        self.assertEqual(g, [["pdf"]])

    def test_untranslatable_input_fails_loudly(self):
        with self.assertRaises(ValueError):
            D.parse_terms(purpose="가나다라마바사")
        with self.assertRaises(ValueError):
            D.parse_terms(terms_arg="노동, law")

    def test_synonym_syntax(self):
        self.assertEqual(D.parse_terms(terms_arg="labor|employment, law"), [["labor", "employment"], ["law"]])

    def test_ladder_has_no_parenthesised_or(self):
        for _, q in D.build_ladder([["a", "b"], ["c"], ["d"]]):
            self.assertNotIn("(", q)
            self.assertNotIn(" OR ", q)

    def test_ladder_goes_strict_to_loose_and_is_capped(self):
        lad = D.build_ladder([["a"], ["b"], ["c"], ["d"]], max_queries=7)
        self.assertEqual(lad[0][0], "strict")
        self.assertLessEqual(len(lad), 7)

    def test_relaxes_until_something_relevant_is_found(self):
        groups = [["korea"], ["labor"], ["law"], ["compliance"]]
        hit = repo("kr/korean-labor-law", 40, "korea labor law checker", ["korea", "labor", "law"])
        # strict (4 words) finds nothing; only the pair query 'korea labor' returns the repo
        fake = FakeGH({"korea labor in:": [hit]})
        repos, trace = D.search_with_relaxation(fake, groups, target=1, relevant=0.4)
        self.assertEqual([r["full_name"] for r in repos], ["kr/korean-labor-law"])
        self.assertEqual(trace[0]["rung"], "strict")
        self.assertEqual(trace[0]["returned"], 0)
        self.assertGreater(len(trace), 1)
        self.assertEqual(trace[-1]["rung"], "pairs")

    def test_stops_early_when_enough_found(self):
        groups = [["a"], ["b"]]
        hits = [repo(f"o/r{i}", 5, "a b", ["a", "b"]) for i in range(5)]
        fake = FakeGH({"a b in:": hits})
        _, trace = D.search_with_relaxation(fake, groups, target=3, relevant=0.4)
        self.assertEqual(len(trace), 1)

    def test_search_error_is_reported_not_swallowed(self):
        class Boom(FakeGH):
            def search(self, q, per_page=30, sort=None):
                raise RuntimeError("rate limit")
        _, trace = D.search_with_relaxation(Boom({}), [["a"], ["b"]], max_queries=3)
        self.assertTrue(all("error" in t for t in trace))


if __name__ == "__main__":
    unittest.main()
