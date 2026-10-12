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


class SentenceHandling(unittest.TestCase):
    """Realistic sentences: filler is dropped, nothing silently disappears, odd input is treated the same everywhere."""

    def words(self, text):
        return [g[0] for g in D.interpret(purpose=text)["groups"]]

    def test_english_filler_does_not_use_up_the_term_slots(self):
        self.assertEqual(self.words("I am looking for a tool that can help me translate PDF documents into Korean"),
                         ["translate", "pdf", "documents", "korean"])
        self.assertEqual(self.words("Can you find me a good MCP server for searching the web?"),
                         ["mcp", "server", "searching", "web"])

    def test_cut_groups_are_reported(self):
        r = D.interpret(purpose="alpha beta gamma delta epsilon zeta eta theta")
        self.assertEqual(len(r["groups"]), D.MAX_GROUPS)
        self.assertEqual([g[0] for g in r["dropped"]], ["eta", "theta"])

    def test_unknown_korean_is_reported_not_swallowed(self):
        r = D.interpret(purpose="pdf 렌더링")
        self.assertEqual(r["ignored"], ["렌더링"])
        self.assertEqual(self.words("PDF 번역 도구"), ["pdf", "translate"])  # 번역 is known now, 도구 is filler
        self.assertEqual(D.interpret(purpose="PDF 번역 도구")["ignored"], [])

    def test_nothing_understood_names_the_words(self):
        with self.assertRaisesRegex(ValueError, "Not understood"):
            D.interpret(purpose="노무현 연설문")

    def test_terms_keep_digits_symbols_and_drop_trailing_punctuation(self):
        self.assertEqual(self.words("3D printing"), ["3d", "printing"])
        self.assertEqual(self.words("C# parser"), ["c#", "parser"])
        self.assertEqual(self.words("R language"), ["r", "language"])
        self.assertEqual(self.words("node.js library"), ["node.js", "library"])
        self.assertEqual(self.words("law. labor."), ["law", "labor"])
        self.assertEqual(self.words("year 2024 report"), ["year", "report"])  # a bare number is noise

    def test_repeated_groups_count_once(self):
        self.assertEqual(D.parse_terms(terms_arg="law|law, law"), [["law"]])
        self.assertEqual(D.parse_terms(terms_arg="law|statute, statute|law"), [["law", "statute"]])

    def test_blank_characters_are_the_same_in_both_implementations(self):
        for blank in (chr(0x1c), chr(0x85), chr(0xfeff), chr(0xa0), chr(0x3000)):
            self.assertEqual(D.parse_terms(terms_arg="law," + blank), [["law"]], hex(ord(blank)))

    def test_short_words_do_not_match_by_stripping_an_s(self):
        toks = D.tokens("a diagram as code. it is news for ios in js. new tools")
        self.assertFalse(D.word_in("as", {"a"}))
        self.assertFalse(D.word_in("its", {"it"}))
        self.assertFalse(D.word_in("news", {"new"}))
        self.assertFalse(D.word_in("ios", {"io"}))
        self.assertTrue(D.word_in("tools", {"tool"}))   # a real plural still matches
        self.assertTrue(D.word_in("pdf", {"pdfs"}))     # and the other direction too
        self.assertTrue(D.word_in("as", toks))          # the word itself still matches

    def test_a_word_no_repository_has_is_left_out_not_made_decisive(self):
        pool = [repo(f"o/pdf-{i}", 10 + i, "pdf translation", ["pdf"]) for i in range(12)]
        base, _ = D.rank(pool, [["pdf"], ["translation"]], min_rel=0.34, now=NOW)
        with_absent, _ = D.rank(pool, [["pdf"], ["translation"], ["xyzzy"]], min_rel=0.34, now=NOW)
        self.assertEqual(len(with_absent), len(base))
        self.assertEqual([r["rel"] for r in with_absent], [r["rel"] for r in base])  # same scores, absent word ignored
        self.assertEqual(D.unmatched_groups(pool, [["pdf"], ["xyzzy"]]), [["xyzzy"]])
        self.assertEqual(D.idf_weights(pool, [["pdf"], ["xyzzy"]])[1], 0.0)

    def test_small_pools_keep_equal_weights(self):
        pool = [repo("o/pdf", 1, "pdf", ["pdf"])]
        self.assertEqual(D.idf_weights(pool, [["pdf"], ["xyzzy"]]), [1.0, 1.0])
        self.assertEqual(D.unmatched_groups(pool, [["xyzzy"]]), [])

    def test_only_github_timestamps_count_as_dates(self):
        ok = "2026-10-01T00:00:00Z"
        self.assertEqual(D.days_since(ok, NOW), 11)
        for bad in ("2026-10-01", "2026-10-01T00:00:00", "bad", "2026-13-45T00:00:00Z", "2026-02-30T00:00:00Z",
                    "2026-01-01T24:00:00Z", "2026-10-01T00:00:60Z", "0001-01-01T00:00:00Z", ok + "\n", "", None):
            self.assertEqual(D.days_since(bad, NOW), 9999, repr(bad))
        self.assertEqual(D.days_since("2099-01-01T00:00:00Z", NOW), 0)  # the future is "today", not an error

    def test_min_rel_default_is_the_shared_constant(self):
        self.assertEqual(D.MIN_REL, 0.34)


class PositionBasedMatching(unittest.TestCase):
    """A glossary word claims the characters it explains, wherever it stands (found by the regression review)."""

    def words(self, text):
        return [g[0] for g in D.interpret(purpose=text)["groups"]]

    def test_a_short_word_survives_next_to_a_longer_word_that_contains_it(self):
        self.assertIn("law", self.words("노동법 준수 방법"))      # 법 of 노동법 is not the 법 of 방법
        self.assertIn("law", self.words("법인 노동법"))
        self.assertEqual(self.words("데이터베이스 데이터"), ["database", "data"])

    def test_words_that_merely_contain_a_key_do_not_trigger_it(self):
        for text in ("한국어 맞춤법 검사", "기법 모음", "pdf 수법", "pdf 법칙", "pdf 공법"):
            self.assertNotIn("law", [w for g in D.interpret(purpose="pdf " + text)["groups"] for w in g], text)
        self.assertNotIn("hr", self.words("pdf 인사말"))
        self.assertNotIn("wage", self.words("pdf 임금님"))
        self.assertEqual(self.words("바코드 스캐너 pdf")[0], "barcode")
        self.assertEqual(self.words("서버리스 배포")[0], "serverless")

    def test_an_ascii_key_is_not_a_piece_of_a_longer_english_word(self):
        self.assertEqual(self.words("ERPNext"), ["erpnext"])
        self.assertEqual(self.words("회계 ERP invoice"), ["accounting", "erp", "invoice"])

    def test_nothing_but_weak_words_is_still_a_search(self):
        self.assertEqual(self.words("project management"), ["project", "management"])
        self.assertEqual(self.words("software tool"), ["software", "tool"])
        with self.assertRaises(ValueError):
            D.interpret(purpose="who is")

    def test_removed_filler_that_could_have_been_a_term_is_reported(self):
        r = D.interpret(purpose="I am looking for a tool that can help me translate PDF documents into Korean")
        self.assertEqual(r["omitted"], ["looking", "help", "tool"])  # grammar words are not worth mentioning
        self.assertEqual(D.interpret(purpose="help desk software")["omitted"], ["help", "software"])
        self.assertEqual(D.interpret(purpose="pdf translation")["omitted"], [])

    def test_plural_s_is_added_only_to_words_of_three_letters_or_more(self):
        self.assertFalse(D.word_in("r", {"rs"}))
        self.assertFalse(D.word_in("c", {"cs"}))
        self.assertTrue(D.word_in("pdf", {"pdfs"}))
        self.assertTrue(D.word_in("tool", {"tools"}))

    def test_rounding_is_half_up(self):
        self.assertEqual(D.round3(0.8125), 0.813)   # Python's round() gives 0.812: the two languages disagreed
        self.assertEqual(D.round3(0.8124), 0.812)

    def test_unknown_korean_is_measured_in_characters_not_code_units(self):
        astral = chr(0x20000)
        self.assertEqual(D.interpret(purpose="labor law " + astral)["ignored"], [])        # one character
        self.assertEqual(D.interpret(purpose="labor law " + astral * 2)["ignored"], [astral * 2])
        self.assertEqual(D.interpret(purpose="labor law " + chr(0xbd) * 2)["ignored"], [])  # fractions are not letters


class LicensePenalty(unittest.TestCase):
    def score(self, lic, allowed):
        return D.score_repo(repo("o/x", 10, "labor law", ["labor"], lic=lic), [["labor"], ["law"]], allowed, now=NOW)["score"]

    def test_with_an_allowed_list_unknown_is_penalised_like_an_unlisted_license(self):
        ok = self.score("MIT", ["MIT"])
        for lic in (None, "NOASSERTION", "ISC"):
            self.assertLess(self.score(lic, ["MIT"]), ok * 0.7, lic)

    def test_without_a_list_unknown_keeps_its_small_discount_only(self):
        self.assertGreater(self.score(None, []), self.score("MIT", []) * 0.95)

    def test_permissive_keyword_and_plain_lists(self):
        self.assertEqual(D.license_list("permissive"), D.PERMISSIVE)
        self.assertEqual(D.license_list(" MIT , ISC ,"), ["MIT", "ISC"])
        self.assertEqual(D.license_list(""), [])
        self.assertIn("MIT-0", D.PERMISSIVE)


class FailureHandling(unittest.TestCase):
    def test_a_missing_gh_is_a_clear_error_not_a_traceback(self):
        from unittest import mock
        with mock.patch.object(D.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "gh CLI was not found"):
                D.GH()._run(["api", "user"])

    def test_non_json_output_is_a_clear_error(self):
        from unittest import mock
        done = mock.Mock(returncode=0, stdout="<html>", stderr="")
        with mock.patch.object(D.subprocess, "run", return_value=done):
            with self.assertRaisesRegex(RuntimeError, "not JSON"):
                D.GH()._run(["api", "user"])

    def test_a_rate_limit_is_waited_for_once_per_run_not_once_per_query(self):
        from unittest import mock
        limited = mock.Mock(returncode=1, stdout="", stderr="API rate limit exceeded")
        gh = D.GH(pace=0)
        with mock.patch.object(D.subprocess, "run", return_value=limited), mock.patch.object(D.time, "sleep") as sleep:
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    gh._run(["api", "x"])
        self.assertEqual(sleep.call_count, 1)

    def test_searches_stop_after_three_failures_in_a_row_and_say_so(self):
        class Down:
            calls = 0

            def search(self, q, per_page=30, sort=None):
                Down.calls += 1
                raise RuntimeError("HTTP 401: Bad credentials")
        _, trace = D.search_with_relaxation(Down(), [["a"], ["b"], ["c"]], max_queries=16)
        self.assertEqual(Down.calls, D.MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(trace[-1]["rung"], "stopped")
        self.assertTrue(D.search_failed(trace))

    def test_a_failed_search_blames_the_login_not_the_terms(self):
        from types import SimpleNamespace
        trace = [{"rung": "strict", "query": "q", "error": "HTTP 401: Bad credentials"}]
        args = SimpleNamespace(license="", avoid="", min_rel=D.MIN_REL, top=10)
        text, _ = D.report_block("t", [], [["a"]], args, trace)
        self.assertIn("The search itself failed", text)
        self.assertIn("Bad credentials", text)
        self.assertNotIn("Loosen", text)

    def test_an_unknown_source_is_an_error_not_a_silent_no_op(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(D.main(["--terms", "a,b", "--source", "star"]), 2)
        self.assertIn("unknown --source star", err.getvalue())

    def test_terms_win_over_purpose_and_the_user_is_told(self):
        import contextlib
        import io
        from unittest import mock

        class Quiet:
            def starred(self, refresh=False):
                return []

            def search(self, *a, **k):
                return []
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(D, "GH", Quiet), contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            D.main(["--terms", "korea, law", "--purpose", "pdf", "--source", "stars"])
        self.assertIn("--purpose is ignored", err.getvalue())
        self.assertIn("'korea'", out.getvalue())


class ExitCodeAndOfflineUse(unittest.TestCase):
    def test_when_every_source_fails_the_exit_code_says_so(self):
        import contextlib
        import io
        from unittest import mock

        class Down:
            def starred(self, refresh=False):
                raise RuntimeError("no network")

            def search(self, *a, **k):
                raise RuntimeError("no network")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(D, "GH", Down), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = D.main(["--terms", "korea, law"])
        self.assertEqual(code, 1)
        self.assertIn("no network", out.getvalue())

    def test_no_matches_is_not_a_failure(self):
        import contextlib
        import io
        from unittest import mock

        class Empty:
            def starred(self, refresh=False):
                return []

            def search(self, *a, **k):
                return []
        with mock.patch.object(D, "GH", Empty), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(D.main(["--terms", "korea, law"]), 0)


class StarredCache(unittest.TestCase):
    class Recorded(D.GH):
        def __init__(self, cache_dir, login="me"):
            super().__init__(pace=0, cache_dir=cache_dir)
            self.login_name, self.fetches = login, 0

        def _run(self, args, retries=1):
            if args == ["api", "user"]:
                return {"login": self.login_name}
            self.fetches += 1
            return [[{"full_name": "o/r", "description": "d", "stargazers_count": 1}]]

    def test_second_call_uses_the_cache_for_the_same_account(self):
        with tempfile.TemporaryDirectory() as d:
            gh = self.Recorded(d)
            self.assertEqual(len(gh.starred()), 1)
            gh.starred()
            self.assertEqual(gh.fetches, 1)

    def test_another_account_never_sees_the_previous_list(self):
        with tempfile.TemporaryDirectory() as d:
            self.Recorded(d, "alice").starred()
            bob = self.Recorded(d, "bob")
            bob.starred()
            self.assertEqual(bob.fetches, 1)

    def test_a_fresh_cache_still_works_when_github_cannot_be_reached_for_the_account_check(self):
        with tempfile.TemporaryDirectory() as d:
            self.Recorded(d).starred()

            class Offline(self.Recorded):
                def _run(self, args, retries=1):
                    if args == ["api", "user"]:
                        raise RuntimeError("offline")
                    return super()._run(args, retries)
            gh = Offline(d)
            self.assertEqual([r["full_name"] for r in gh.starred()], ["o/r"])
            self.assertEqual(gh.fetches, 0)

    def test_odd_entries_in_the_reply_are_skipped(self):
        class Odd(self.Recorded):
            def _run(self, args, retries=1):
                if args == ["api", "user"]:
                    return {"login": "me"}
                return [None, [None, "text", {"full_name": "o/ok", "stargazers_count": 3}]]
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual([r["full_name"] for r in Odd(d).starred()], ["o/ok"])

    def test_a_damaged_or_old_format_cache_is_fetched_again(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "starred.json"
            for content in ("{not json", json.dumps([{"full_name": "old/format"}]), json.dumps({"login": "me", "repos": "x"})):
                cache.write_text(content, encoding="utf-8")
                gh = self.Recorded(d)
                self.assertEqual([r["full_name"] for r in gh.starred()], ["o/r"])
                self.assertEqual(gh.fetches, 1, content)


class EnvironmentScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def skill(self, text, name="s", raw=None):
        d = self.home / ".claude" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_bytes(raw if raw is not None else text.encode("utf-8"))
        return d / "SKILL.md"

    def test_folded_and_literal_block_values_are_read(self):
        folded = self.skill("---\nname: a\ndescription: >\n  Deploys the app\n  to production.\n---\nbody\n", "a")
        literal = self.skill("---\nname: b\ndescription: |-\n  First line\n  second line\n---\n", "b")
        self.assertEqual(S.frontmatter(folded)["description"], "Deploys the app to production.")
        self.assertEqual(S.frontmatter(literal)["description"], "First line second line")

    def test_plain_values_wrapped_over_several_lines_are_joined(self):
        p = self.skill("---\nname: c\ndescription: Reviews pull requests\n  and writes a summary\n---\n", "c")
        self.assertEqual(S.frontmatter(p)["description"], "Reviews pull requests and writes a summary")

    def test_a_byte_order_mark_does_not_hide_the_header(self):
        p = self.skill("", "d", raw=b"\xef\xbb\xbf---\nname: d\ndescription: with a BOM\n---\n")
        self.assertEqual(S.frontmatter(p), {"name": "d", "description": "with a BOM"})

    def test_a_long_description_is_read_whole(self):
        p = self.skill("---\nname: e\ndescription: " + "word " * 1200 + "\n---\n", "e")
        self.assertEqual(len(S.frontmatter(p)["description"]), len(("word " * 1200).strip()))

    def test_quoted_values_and_nested_keys_do_not_break_the_header(self):
        p = self.skill("---\nname: 'f'\nmetadata:\n  type: user\ndescription: \"quoted\"\n---\n", "f")
        h = S.frontmatter(p)
        self.assertEqual((h["name"], h["description"]), ("f", "quoted"))

    def test_a_value_wrapped_over_lines_loses_its_closing_quote_and_comments_are_skipped(self):
        p = self.skill("---\nname: g\ndescription: \"first\n  second\"\n---\n", "g")
        self.assertEqual(S.frontmatter(p)["description"], "first second")
        q = self.skill("---\nname: h\ndescription: >\n  alpha\n  # a comment\n  beta\n---\n", "h")
        self.assertEqual(S.frontmatter(q)["description"], "alpha beta")

    def test_an_agent_in_several_cached_versions_is_counted_once(self):
        for version in ("1.0", "1.1"):
            d = self.home / ".claude" / "plugins" / "cache" / "mkt" / "kit" / version / "agents"
            d.mkdir(parents=True)
            (d / "reviewer.md").write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        agents = [a for a in S.scan_agents(self.home) if a["name"] == "reviewer"]
        self.assertEqual(len(agents), 1)

    def test_agents_of_plugins_in_the_cache_are_counted(self):
        d = self.home / ".claude" / "plugins" / "cache" / "mkt" / "toolkit" / "1.0" / "agents"
        d.mkdir(parents=True)
        (d / "reviewer.md").write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        self.assertIn({"name": "reviewer", "source": "plugin:toolkit"}, S.scan_agents(self.home))

    def test_the_inventory_file_is_created_with_its_folder_and_private(self):
        out = self.home / "new" / "folder" / "env.json"
        S.write_private(out, {"a": 1})
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"a": 1})
        if os.name == "posix":
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            out.chmod(0o644)  # an existing, wider file is tightened too
            S.write_private(out, {"a": 2})
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
