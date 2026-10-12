#!/usr/bin/env python3
"""discover.py - find candidate GitHub repos for a purpose (stage b of repo-fit-check).

Backend is the `gh` CLI only (no GitHub MCP). Ranking is relevance-first; stars are a
capped, log-scaled tie-breaker, so a 40-star repo that fits beats a 100k-star one that doesn't.

Sources
  --source stars    the authenticated user's starred repos (cached 24h)
  --source search   GitHub repository search, relaxing the query step by step until enough
                    relevant candidates are found (never silently returns nothing)
  --gaps ENV.json   search for categories that scan_env.py reported as gap/weak

Examples
  python discover.py --purpose "한국 노동 법령 준수 점검" --stage 구현
  python discover.py --terms "labor|employment, law|statute, compliance" --license MIT,Apache-2.0 --avoid AGPL
  python discover.py --gaps env.json
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
REFS = HERE.parent / "references"
STOPWORDS = {"a", "an", "the", "and", "or", "for", "of", "to", "in", "on", "with", "my", "our", "tool", "tools",
             "that", "this", "using", "use", "app", "project"}
GENERIC = {"platform", "system", "service", "program", "software", "solution", "application", "management"}
FIELD_WEIGHT = {"name": 1.0, "topics": 0.9, "description": 0.7}
QUALIFIERS = "in:name,description,readme archived:false fork:false"


# ---------------------------------------------------------------- terms
def load_glossary():
    try:
        with open(REFS / "ko_glossary.json", encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    except (OSError, ValueError):
        return {}


def has_non_ascii(text):
    return any(ord(c) > 127 for c in text)


def parse_terms(terms_arg=None, purpose=None, glossary=None):
    """Return term groups: [['labor','employment'], ['law','statute'], ['compliance']].
    Within a group words are synonyms (any one matches); groups are all required for full relevance.
    Raises ValueError when only untranslatable non-ASCII text is available (fail loudly, not empty)."""
    glossary = load_glossary() if glossary is None else glossary
    groups = []
    if terms_arg:
        for part in terms_arg.split(","):
            words = [w.strip().lower() for w in part.split("|") if w.strip()]
            if words:
                groups.append(words)
    elif purpose:
        used, found = set(), []
        for ko, en in sorted(glossary.items(), key=lambda kv: -len(kv[0])):
            if ko in purpose and not any(ko in u or u in ko for u in used):
                used.add(ko)
                if en:  # an empty value means "ignore this word" (방법 must not trigger 법)
                    found.append((purpose.index(ko), [w.lower() for w in en.split("|")]))
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9\-\.]+", purpose):
            w = m.group(0).lower()
            if w not in STOPWORDS and not any(w in g for _, g in found):
                found.append((m.start(), [w]))
        # purpose order = user's emphasis; generic words ('platform') only dilute relevance
        groups = [g for _, g in sorted(found, key=lambda x: x[0]) if not set(g) <= GENERIC]
    bad = [w for g in groups for w in g if has_non_ascii(w)]
    if bad:
        raise ValueError(f"non-English terms need translation: {bad}. Pass English --terms 'a|b, c'.")
    if not groups:
        raise ValueError("no usable search terms. Pass --terms 'labor|employment, law, compliance' "
                         "(English; use | for synonyms) or an --purpose the glossary understands.")
    return groups[:6]


# ---------------------------------------------------------------- gh backend
class GH:
    """Thin wrapper over the gh CLI; tests substitute a fake with the same interface."""

    def __init__(self, pace=1.2, cache_dir=None):
        self.pace = pace
        # per-user dir: the system temp dir can be a shared folder on some machines
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "repo-fit-check")
        self.queries = 0

    def _run(self, args, retries=1):
        for attempt in range(retries + 1):
            p = subprocess.run(["gh"] + args, capture_output=True, text=True, encoding="utf-8")
            if p.returncode == 0:
                return json.loads(p.stdout or "null")
            err = (p.stderr or "") + (p.stdout or "")
            if attempt < retries and re.search(r"rate limit|secondary|403", err, re.I):
                time.sleep(30)
                continue
            raise RuntimeError(err.strip()[:300])

    def search(self, q, per_page=30, sort=None):
        time.sleep(self.pace if self.queries else 0)
        self.queries += 1
        args = ["api", "-X", "GET", "search/repositories", "-f", f"q={q}", "-f", f"per_page={per_page}"]
        if sort:
            args += ["-f", f"sort={sort}", "-f", "order=desc"]
        return [normalize(r) for r in (self._run(args) or {}).get("items", [])]

    def starred(self, ttl_hours=24, refresh=False):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache = self.cache_dir / "starred.json"
        if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < ttl_hours * 3600:
            return json.loads(cache.read_text(encoding="utf-8"))
        pages = self._run(["api", "user/starred?per_page=100", "--paginate", "--slurp"]) or []
        repos = [normalize(r) for page in pages for r in page]
        cache.write_text(json.dumps(repos, ensure_ascii=False), encoding="utf-8")
        return repos


def normalize(r):
    lic = (r.get("license") or {}).get("spdx_id")
    return {
        "full_name": r.get("full_name"), "description": r.get("description") or "",
        "stars": r.get("stargazers_count", 0), "pushed_at": r.get("pushed_at"),
        "license": lic, "archived": bool(r.get("archived")), "fork": bool(r.get("fork")),
        "language": r.get("language"), "topics": r.get("topics") or [], "url": r.get("html_url"),
        "open_issues": r.get("open_issues_count"),
    }


# ---------------------------------------------------------------- query ladder
def _q(words, extra=""):
    parts = [f'"{w}"' if " " in w else w for w in words]
    return " ".join(parts + [QUALIFIERS] + ([extra] if extra else []))


def build_ladder(groups, extra="", max_queries=16):
    """Yield (rung, query) from strict to loose. Avoids parenthesised OR, which GitHub's repo
    search does not apply reliably; synonyms are expanded as separate queries instead."""
    out, seen = [], set()

    def add(rung, q):
        if q not in seen and len(out) < max_queries:
            seen.add(q)
            out.append((rung, q))

    first = [g[0] for g in groups]
    topic = lambda *ws: " ".join(f"topic:{w}" for w in ws) + f" archived:false fork:false {extra}".rstrip()
    add("strict", _q(first, extra))
    if len(groups) >= 3:  # dropping one word at a time recovers most AND-too-strict misses
        for i in range(len(groups)):
            add("drop-one", _q(first[:i] + first[i + 1:], extra))
    if len(groups) >= 2:  # hyphenated topics such as 'labor-law' are very precise on niche domains
        add("topics", topic("-".join(first[:2])))
        add("topics", topic(*[g[0] for g in groups[:2]]))
    for a, b in combinations(range(len(groups)), 2):
        add("pairs", _q([first[a], first[b]], extra))
    for i, g in enumerate(groups):  # synonym swaps, one group at a time
        for alt in g[1:]:
            add("synonyms", _q(first[:i] + [alt] + first[i + 1:], extra))
    for g in groups:
        for w in g[:2]:
            add("topics", topic(w))
    for g in groups:
        add("singles", _q([g[0]], extra))
    return out


# ---------------------------------------------------------------- scoring
def tokens(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def word_in(word, toks):
    parts = re.findall(r"[a-z0-9]+", word.lower())
    if not parts:
        return False
    def has(p):
        return p in toks or p + "s" in toks or (p.endswith("s") and p[:-1] in toks)
    return all(has(p) for p in parts)


def group_scores(repo, groups):
    """Per group: best field weight among fields matching any synonym, and which field."""
    fields = {"name": tokens(repo["full_name"].split("/")[-1]) | tokens(repo["full_name"].split("/")[0]),
              "topics": tokens(" ".join(repo["topics"])), "description": tokens(repo["description"])}
    scores, wheres = [], []
    for g in groups:
        best, where = 0.0, None
        for fname, weight in FIELD_WEIGHT.items():
            if weight > best and any(word_in(w, fields[fname]) for w in g):
                best, where = weight, fname
        scores.append(best)
        wheres.append(where)
    return scores, wheres


def idf_weights(repos, groups):
    """Rarer term groups in the pool discriminate more ('korea' > 'compliance')."""
    n = len(repos)
    if n < 10:
        return [1.0] * len(groups)
    df = [0] * len(groups)
    for r in repos:
        for i, s in enumerate(group_scores(r, groups)[0]):
            df[i] += 1 if s > 0 else 0
    return [math.log(1 + n / (d + 1)) for d in df]


def relevance(repo, groups, weights=None):
    """Weighted mean of group scores; plus 'why' for display."""
    scores, wheres = group_scores(repo, groups)
    weights = weights or [1.0] * len(groups)
    rel = sum(w * s for w, s in zip(weights, scores)) / sum(weights)
    if all(s > 0 for s in scores):
        rel = min(1.0, rel + 0.1)
    why = [f"{g[0]}<-{where}" for g, where in zip(groups, wheres) if where]
    return rel, why


def days_since(iso, now=None):
    if not iso:
        return 9999
    now = now or datetime.now(timezone.utc)
    return max(0, (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days)


def activity(days):
    return 1.0 if days <= 30 else 0.8 if days <= 90 else 0.5 if days <= 180 else 0.2 if days <= 365 else 0.0


def license_status(spdx, allowed, avoid):
    s = (spdx or "").upper()
    if any(a.upper() in s for a in avoid if s):
        return "blocked"
    if not s or s in ("NOASSERTION", "OTHER"):
        return "unknown"
    if allowed and s not in {a.upper() for a in allowed}:
        return "mismatch"
    return "ok"


def score_repo(repo, groups, allowed=(), avoid=(), now=None, weights=None):
    rel, why = relevance(repo, groups, weights)
    days = days_since(repo["pushed_at"], now)
    pop = min(1.0, math.log10(repo["stars"] + 1) / 5)
    lic = license_status(repo["license"], allowed, avoid)
    lic_score = {"ok": 1.0, "unknown": 0.5, "mismatch": 0.0, "blocked": 0.0}[lic]
    final = 0.70 * rel + 0.10 * pop + 0.15 * activity(days) + 0.05 * lic_score
    if lic == "mismatch":
        final *= 0.6
    return {**repo, "rel": round(rel, 3), "pop": round(pop, 3), "days": days, "lic_status": lic,
            "score": round(final, 3), "why": why}


def rank(repos, groups, allowed=(), avoid=(), min_rel=0.0, now=None):
    seen, scored, excluded = set(), [], []
    weights = idf_weights([r for r in repos if not (r["archived"] or r["fork"])], groups)
    for r in repos:
        if not r["full_name"] or r["full_name"] in seen:
            continue
        seen.add(r["full_name"])
        if r["archived"] or r["fork"]:
            excluded.append((r["full_name"], "archived/fork"))
            continue
        s = score_repo(r, groups, allowed, avoid, now, weights)
        if s["lic_status"] == "blocked":
            excluded.append((r["full_name"], f"license {r['license']} is in --avoid"))
            continue
        if s["rel"] >= min_rel:
            scored.append(s)
    scored.sort(key=lambda x: (-x["score"], -x["stars"]))
    return scored, excluded


def select(scored, top=10, small_picks=5, small_stars=500, small_min_rel=0.5):
    """Top by score, then separately surface relevant small repos so big ones can't crowd them out."""
    main = scored[:top]
    names = {r["full_name"] for r in main}
    small = [r for r in scored if r["stars"] < small_stars and r["rel"] >= small_min_rel and r["full_name"] not in names]
    return main, small[:small_picks]


# ---------------------------------------------------------------- search driver
def search_with_relaxation(gh, groups, extra="", target=15, relevant=0.45, max_queries=16, allowed=(), avoid=()):
    """Run the ladder until `target` relevant (rel>=relevant) candidates exist. Returns (repos, trace)."""
    pool, trace = {}, []
    for rung, q in build_ladder(groups, extra, max_queries):
        try:
            found = gh.search(q)
        except RuntimeError as e:
            trace.append({"rung": rung, "query": q, "error": str(e)[:120]})
            continue
        new = 0
        for r in found:
            if r["full_name"] not in pool:
                pool[r["full_name"]] = r
                new += 1
        scored, _ = rank(list(pool.values()), groups, allowed, avoid, min_rel=relevant)
        trace.append({"rung": rung, "query": q, "returned": len(found), "new": new, "relevant_total": len(scored)})
        if len(scored) >= target:
            break
    return list(pool.values()), trace


# ---------------------------------------------------------------- output
def fmt_table(rows):
    lines = ["| # | repo | stars | pushed | license | rel | score | matched |", "|---|---|---:|---:|---|---:|---:|---|"]
    for i, r in enumerate(rows, 1):
        lic = r["license"] or "none"
        if r["lic_status"] in ("mismatch", "unknown"):
            lic += f" ({r['lic_status']})"
        lines.append(f"| {i} | {r['full_name']} | {r['stars']:,} | {r['days']}d | {lic} | {r['rel']:.2f} | {r['score']:.2f} | "
                     f"{', '.join(r['why']) or '-'} |")
    return "\n".join(lines)


def report_block(title, repos, groups, args, trace=None):
    allowed = [x for x in args.license.split(",") if x] if args.license else []
    avoid = [x for x in args.avoid.split(",") if x] if args.avoid else []
    scored, excluded = rank(repos, groups, allowed, avoid, min_rel=args.min_rel)
    main, small = select(scored, args.top)
    out = [f"### {title}", f"pool {len(repos)} -> kept {len(scored)} (excluded {len(excluded)})"]
    if not main:
        out.append("No relevant candidate. Loosen --terms or add synonyms; the query trace below shows what was tried.")
    else:
        out.append(fmt_table(main))
    if small:
        out.append("\nSmall repos (<500 stars) that also match, not crowded out by big ones:")
        out.append(fmt_table(small))
    if excluded:
        out.append("\nExcluded: " + "; ".join(f"{n} ({why})" for n, why in excluded[:8]) + (" ..." if len(excluded) > 8 else ""))
    if trace:
        out.append("\nQuery trace (rung -> returned/new/relevant_total):")
        for t in trace:
            out.append(f"- {t['rung']}: `{t['query'][:90]}` -> " + (t.get("error") and "ERROR " + t["error"] or
                       f"{t['returned']}/{t['new']}/{t['relevant_total']}"))
    return "\n".join(out), main + small


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--purpose", help="free text (Korean OK); glossary maps known words to English")
    ap.add_argument("--terms", help="English term groups: 'labor|employment, law|statute, compliance'")
    ap.add_argument("--stage", default="", help="informational; echoed in the report")
    ap.add_argument("--source", default="stars,search", help="comma list of: stars, search")
    ap.add_argument("--gaps", help="env.json from scan_env.py --out; search categories that are gap/weak")
    ap.add_argument("--license", default="", help="allowed SPDX ids, comma separated (soft: mismatches are penalised)")
    ap.add_argument("--avoid", default="", help="license substrings to exclude, e.g. AGPL,GPL-3")
    ap.add_argument("--language", default="")
    ap.add_argument("--active-days", type=int, default=0, help="only search repos pushed within N days")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-rel", type=float, default=0.34)
    ap.add_argument("--max-queries", type=int, default=16)
    ap.add_argument("--refresh-stars", action="store_true")
    ap.add_argument("--json-out")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    gh = GH()
    extra = f"language:{args.language}" if args.language else ""
    if args.active_days:
        cutoff = datetime.fromordinal(datetime.now(timezone.utc).date().toordinal() - args.active_days).strftime("%Y-%m-%d")
        extra = (extra + f" pushed:>{cutoff}").strip()
    allowed = [x for x in args.license.split(",") if x]
    avoid = [x for x in args.avoid.split(",") if x]
    blocks, collected = [], {}

    if args.gaps:
        env = json.load(open(args.gaps, encoding="utf-8"))
        targets = env["coverage"]["gaps"] + [c for c in env["coverage"]["covered"] if c.get("confidence") == "weak"]
        if not targets:
            blocks.append("No gap or weak category in env.json: nothing to search for.")
        for cat in targets[:4]:
            groups = [[w.strip() for w in g.split("|") if w.strip()] for g in cat.get("search") or []]
            if not groups:
                continue
            repos, trace = search_with_relaxation(gh, groups, extra, target=8, max_queries=6, allowed=allowed, avoid=avoid)
            text, picks = report_block(f"Gap: {cat['label']}", repos, groups, args, trace)
            blocks.append(text)
            collected[cat["id"]] = picks
    else:
        try:
            groups = parse_terms(args.terms, args.purpose)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(f"terms: {groups}" + (f"  stage: {args.stage}" if args.stage else ""))
        sources = [s.strip() for s in args.source.split(",")]
        if "stars" in sources:
            try:
                starred = gh.starred(refresh=args.refresh_stars)
                text, picks = report_block(f"From your starred repos ({len(starred)} scanned)", starred, groups, args)
                blocks.append(text)
                collected["stars"] = picks
            except RuntimeError as e:
                blocks.append(f"### From your starred repos\nERROR: {e}")
        if "search" in sources:
            repos, trace = search_with_relaxation(gh, groups, extra, max_queries=args.max_queries, allowed=allowed, avoid=avoid)
            text, picks = report_block("New candidates from GitHub search", repos, groups, args, trace)
            blocks.append(text)
            collected["search"] = picks
    print("\n\n".join(blocks))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(collected, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
