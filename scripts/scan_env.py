#!/usr/bin/env python3
"""Inventory the real Claude Code / Codex environment and find capability gaps.

Reads names and descriptions only. Config values (tokens, headers, env blocks)
are never read into the output: for MCP configs only the server *names* are kept.

Usage:
  python scan_env.py [--home DIR] [--live FILE] [--categories FILE] [--out FILE]

--live FILE  one name per line for things visible in the running session but not
             on disk (claude.ai connectors, deferred tools). Prefix with
             'skill:', 'mcp:' or 'plugin:' to set the kind (default: mcp).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv"}
FRONTMATTER = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---", re.S)
BLIND_SPOTS = [
    "claude.ai connectors (Notion, Figma, ...) are account-level; disk only records 'ever connected' names",
    "Deferred/tool-search tools in the live session are not on disk; pass them with --live",
    "Plugin-bundled MCP servers appear by name only; their auth/connection state is unknown",
]


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


BLOCK_SCALARS = (">", "|", ">-", "|-", ">+", "|+")


def frontmatter(path, limit=32768):
    """Top-level `key: value` pairs of a SKILL.md header. Handles what real skills use: a BOM, quoted
    values, and values continued on indented lines (`description: >` / `|` blocks or plain wrapping)."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:  # utf-8-sig drops a BOM
            head = f.read(limit)
    except OSError:
        return {}
    m = FRONTMATTER.match(head)
    if not m:
        return {}
    lines = m.group(1).splitlines()
    out, i = {}, 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        continued = []
        while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
            continued.append(lines[i].strip())
            i += 1
        continued = [c for c in continued if c and not c.startswith("#")]  # an indented comment is not part of the value
        if value in BLOCK_SCALARS:
            value = ""
        out[key] = " ".join([v for v in [value] + continued if v]).strip("\"'")
    return out


def walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        yield dirpath, filenames


def plugin_name_from_dir(name):
    """'carta-crm~marketplace' -> 'carta-crm'."""
    return name.split("~")[0]


def scan_skills(home):
    """Return skills from user dir, plugin dirs and Codex. Dedup by name, first source wins."""
    claude = Path(home) / ".claude"
    roots = [
        (claude / "skills", "user"),
        (claude / "plugins" / "synced", "plugin"),
        (claude / "plugins" / "cache", "plugin"),
        (Path(home) / ".codex" / "skills", "codex"),
    ]
    seen, skills = set(), []
    for root, source in roots:
        if not root.is_dir():
            continue
        for dirpath, files in walk(root):
            if "SKILL.md" not in files:
                continue
            p = Path(dirpath)
            fm = frontmatter(p / "SKILL.md")
            name = fm.get("name") or p.name
            plugin = None
            rel = p.relative_to(root).parts
            if source == "plugin":
                # synced: <bucket>/<plugin~mkt>/..., cache: <mkt>/<plugin>/<ver>/...
                plugin = plugin_name_from_dir(rel[1]) if len(rel) > 1 else None
            elif source == "user" and rel and rel[0] == "synced":
                source = "user-synced"
            key = (plugin or "", name)
            if key in seen:
                continue
            seen.add(key)
            skills.append({"name": name, "desc": fm.get("description", "")[:300],
                           "source": source if source != "user-synced" else "user", "plugin": plugin})
    return skills


def scan_plugins(home, settings):
    claude = Path(home) / ".claude" / "plugins"
    names = {}

    def add(name, src):
        names.setdefault(name, {"name": name, "sources": set(), "enabled": None})["sources"].add(src)

    synced = claude / "synced"
    if synced.is_dir():
        for bucket in synced.iterdir():
            if bucket.is_dir() and not bucket.name.startswith("."):
                for d in bucket.iterdir():
                    if d.is_dir():
                        add(plugin_name_from_dir(d.name), "synced")
    cache = claude / "cache"
    if cache.is_dir():
        for mkt in cache.iterdir():
            if mkt.is_dir():
                for d in mkt.iterdir():
                    if d.is_dir():
                        add(d.name, "cache")
    data = claude / "data"
    if data.is_dir():
        for d in data.iterdir():
            if d.is_dir():
                add(re.sub(r"-(inline|synced)$", "", d.name), "data")
    for key, on in ((settings or {}).get("enabledPlugins") or {}).items():
        name = key.split("@")[0]
        add(name, "settings")
        names[name]["enabled"] = bool(on)
    out = []
    for v in names.values():
        v["sources"] = sorted(v["sources"])
        out.append(v)
    return sorted(out, key=lambda x: x["name"])


def mcp_names_from_config(data):
    """Server names only. Handles {'mcpServers': {...}} and bare {name: {...}} shapes."""
    if not isinstance(data, dict):
        return []
    block = data.get("mcpServers", data)
    if not isinstance(block, dict):
        return []
    return [k for k, v in block.items() if isinstance(v, dict) and k != "description"]


def scan_mcp(home):
    claude_json = load_json(Path(home) / ".claude.json") or {}
    settings = load_json(Path(home) / ".claude" / "settings.json") or {}
    found = []

    def add(name, source):
        found.append({"name": name, "source": source})

    for n in mcp_names_from_config({"mcpServers": claude_json.get("mcpServers") or {}}):
        add(n, "user-config")
    for proj, cfg in (claude_json.get("projects") or {}).items():
        for n in mcp_names_from_config({"mcpServers": (cfg or {}).get("mcpServers") or {}}):
            add(n, "project:" + os.path.basename(proj.rstrip("/\\")))
    for n in mcp_names_from_config(load_json(Path(home) / ".mcp.json")):
        add(n, "home-.mcp.json")
    for n in mcp_names_from_config({"mcpServers": settings.get("mcpServers") or {}}):
        add(n, "settings")
    for n in claude_json.get("claudeAiMcpEverConnected") or []:
        add(str(n), "claude.ai-connector(ever-connected)")
    for n in claude_json.get("mcpNeedsAuthNoticed") or []:
        add(str(n), "plugin-server(needs-auth)")

    plugins_root = Path(home) / ".claude" / "plugins" / "synced"
    if plugins_root.is_dir():
        for bucket in plugins_root.iterdir():
            if not bucket.is_dir() or bucket.name.startswith("."):
                continue
            for pdir in bucket.iterdir():
                cfg = load_json(pdir / ".mcp.json") if pdir.is_dir() else None
                for n in mcp_names_from_config(cfg):
                    add(f"{plugin_name_from_dir(pdir.name)}:{n}", "plugin-bundled")

    codex_cfg = Path(home) / ".codex" / "config.toml"
    if codex_cfg.is_file():
        try:
            text = codex_cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for m in re.finditer(r"^\[mcp_servers\.([A-Za-z0-9_\-]+)\]", text, re.M):
            add(m.group(1), "codex")
        for m in re.finditer(r'^\[plugins\."?([^"\]\n]+?)"?\]', text, re.M):
            add(m.group(1).split("@")[0], "codex-plugin")

    uniq, seen = [], set()
    for it in found:
        k = (it["name"], it["source"])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    return uniq


def scan_agents(home):
    out = []
    for d, src in ((Path(home) / ".claude" / "agents", "user"),):
        if d.is_dir():
            out += [{"name": p.stem, "source": src} for p in d.glob("*.md")]
    synced = Path(home) / ".claude" / "plugins" / "synced"
    if synced.is_dir():
        for p in synced.glob("*/*/agents/*.md"):
            out.append({"name": p.stem, "source": "plugin:" + plugin_name_from_dir(p.parents[1].name)})
    cache = Path(home) / ".claude" / "plugins" / "cache"
    if cache.is_dir():  # <marketplace>/<plugin>/<version>/agents/<name>.md
        seen = set()
        for p in sorted(cache.glob("*/*/*/agents/*.md")):  # several cached versions of a plugin: count each agent once
            key = (p.parents[2].name, p.stem)
            if key not in seen:
                seen.add(key)
                out.append({"name": p.stem, "source": "plugin:" + p.parents[2].name})
    return out


def read_live(path):
    items = []
    if not path:
        return items
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            kind, name = "mcp", line
            m = re.match(r"(skill|mcp|plugin):(.*)", line)
            if m:
                kind, name = m.group(1), m.group(2).strip()
            items.append({"kind": kind, "name": name})
    return items


def token_pattern(kw):
    norm = re.sub(r"[-_]", " ", kw.lower())
    return re.compile(r"(?<![a-z0-9])" + re.escape(norm) + r"(?![a-z0-9])")


def coverage(corpus_items, categories):
    """corpus_items: list of (label, name, desc). A category is covered only by an item
    whose *name* contains a keyword, or whose description contains >=2 distinct keywords.
    One stray word in a long description is not evidence (that over-credits coverage)."""
    norm = lambda t: re.sub(r"[-_:/.]", " ", t.lower())
    items = [(label, norm(name), norm(desc)) for label, name, desc in corpus_items]
    covered, gaps = [], []
    for cat in categories:
        pats = [token_pattern(k) for k in cat["keywords"]]
        scored, name_items = [], 0
        for label, name, desc in items:
            name_hits = sum(1 for p in pats if p.search(name))
            desc_hits = sum(1 for p in pats if p.search(desc))
            if name_hits or desc_hits >= 2:
                scored.append((name_hits * 3 + desc_hits, label))
                name_items += 1 if name_hits else 0
        if scored:
            scored.sort(key=lambda x: (-x[0], x[1]))
            covered.append({"id": cat["id"], "label": cat["label"], "search": cat.get("search", []),
                            "evidence": [l for _, l in scored[:3]],
                            "matches": len(scored),
                            "confidence": "strong" if name_items >= 3 else "weak"})
        else:
            gaps.append({"id": cat["id"], "label": cat["label"], "search": cat.get("search", [])})
    return covered, gaps


def scan(home, live_path=None, categories_path=None):
    settings = load_json(Path(home) / ".claude" / "settings.json") or {}
    skills = scan_skills(home)
    plugins = scan_plugins(home, settings)
    mcp = scan_mcp(home)
    agents = scan_agents(home)
    live = read_live(live_path)

    cats_file = categories_path or Path(__file__).resolve().parent.parent / "references" / "categories.json"
    categories = (load_json(cats_file) or {}).get("categories", [])

    corpus = [(f"skill:{s['name']}", s["name"], s["desc"]) for s in skills]
    corpus += [(f"plugin:{p['name']}", p["name"], "") for p in plugins if p["enabled"] is not False]
    corpus += [(f"mcp:{m['name']}", m["name"], "") for m in mcp]
    corpus += [(f"agent:{a['name']}", a["name"], "") for a in agents]
    corpus += [(f"live-{i['kind']}:{i['name']}", i["name"], "") for i in live]
    covered, gaps = coverage(corpus, categories)

    def by_source(items, key="source"):
        counts = {}
        for it in items:
            counts[it[key]] = counts.get(it[key], 0) + 1
        return counts

    return {
        "home": str(home),
        "counts": {
            "skills": {"total": len(skills), "by_source": by_source(skills)},
            "plugins": {"total": len(plugins), "enabled_false": [p["name"] for p in plugins if p["enabled"] is False]},
            "mcp": {"total": len(mcp), "by_source": by_source(mcp)},
            "agents": {"total": len(agents)},
            "live": {"total": len(live)},
        },
        "skills": skills, "plugins": plugins, "mcp": mcp, "agents": agents, "live": live,
        "coverage": {"covered": covered, "gaps": gaps},
        "blind_spots": BLIND_SPOTS if not live else BLIND_SPOTS[:1] + BLIND_SPOTS[2:],
    }


def summary(result):
    c = result["counts"]
    lines = [
        "== Environment inventory ==",
        f"skills  : {c['skills']['total']}  {c['skills']['by_source']}",
        f"plugins : {c['plugins']['total']}  (disabled: {c['plugins']['enabled_false'] or '-'})",
        f"mcp     : {c['mcp']['total']}  {c['mcp']['by_source']}",
        f"agents  : {c['agents']['total']}    live-supplied: {c['live']['total']}",
        "",
        f"covered categories: {len(result['coverage']['covered'])}",
    ]
    for cv in result["coverage"]["covered"]:
        tag = "ok  " if cv["confidence"] == "strong" else "weak"
        lines.append(f"  [{tag}] {cv['label']:<30} <- {', '.join(cv['evidence'])}")
    lines.append(f"gap categories: {len(result['coverage']['gaps'])}")
    for g in result["coverage"]["gaps"]:
        lines.append(f"  [gap] {g['label']}")
    lines.append("")
    lines.append("blind spots (not visible from disk):")
    lines += [f"  - {b}" for b in result["blind_spots"]]
    return "\n".join(lines)


def write_private(path, result):
    """The inventory holds the home path, skill descriptions and project folder names: create the folder
    if needed and make the file readable by its owner only (no effect on Windows, where it is a no-op)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    try:
        os.chmod(p, 0o600)  # also when the file already existed with wider permissions
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=os.path.expanduser("~"))
    ap.add_argument("--live")
    ap.add_argument("--categories")
    ap.add_argument("--out", help="write full inventory JSON here")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = scan(args.home, args.live, args.categories)
    if args.out:
        write_private(args.out, result)
    print(summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
