#!/usr/bin/env python3
"""Deterministic pre-verification of an outbound draft. Runs before the draft-verifier agent and
hands it a JSON it can trust, so the agent judges instead of hunting.

    python3 precheck.py <draft.md> [--out prechecks/<slug>.json]

Why. The verifier used to get prose and nothing else, so every run paid three costs in sequence:
decompose prose into claims, search the corpus cold for each, judge. Search dominated, at around
fifty thousand tokens and two minutes for a six line email. And the failure the gate exists for,
a superseded cost figure, was not falsity but STALENESS: the claim matched its source and the
source had been superseded two days earlier. Staleness is a date bounded query over an index that
already exists, not a search.

What this does, all of it without a model:

1. Reads the claims manifest the drafter writes into the notes half of the draft, under a
   `## Claims` heading. Each claim names its evidence and a verbatim quote from it.
2. Checks each claim's text is actually in the body (a manifest that drifted from the body is
   worse than none), that the evidence exists, that the quote is really in it, and pulls the
   evidence date.
3. Runs the staleness query: any knowledge file sharing a topic with the claim and dated AFTER the
   evidence is listed as a candidate the verifier must read. None, and the claim cannot be stale.
4. Extracts every value in the body, identifiers, numbers, dates, domains, capitalised names, and
   greps the corpus for each. A value that appears nowhere is flagged before any model runs. This is
   the class of error where a domain or an identifier range exists nowhere in the corpus.
5. Lists the body lines no claim covers, so the verifier knows what it still has to classify.

The anti anchoring boundary is preserved by construction. The manifest lives in the draft's notes
half, which the verifier is forbidden to open; this script lifts out only claim text, evidence
pointer, quote and dates into the JSON, and the reasoning never travels.

A claim declared `type: platform` is exempt from all of it. A statement about how a cloud
platform behaves in general has no workspace evidence and never will, so demanding a file for
it forces the drafter to point at something that merely repeats the rule. Those are routed to the
judge as PLATFORM_RULE candidates and carry no evidence, quote or topics. The judge still has to
agree it is a platform rule and that it is true; declaring it here only stops the script asking for
evidence that cannot exist. An estate fact dressed as a platform rule is exactly what the judge is
told to catch.

A claim's `topics` must come from the existing vocabulary in INDEX.md. An unknown topic would match
no files, report FRESH, and pass silently, so it is an error here rather than a warning.
"""

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGAGEMENT = os.path.dirname(HERE)
KNOWLEDGE = os.path.join(ENGAGEMENT, "knowledge")
# Optional task tracker for MON-N evidence references. Unset, those references resolve to nothing.
TASK_PY = os.environ.get("TRACKER_PY", "")
TASK_REPO = "project"

sys.path.insert(0, HERE)
sys.path.insert(0, KNOWLEDGE)
import emails as E            # noqa: E402  extract_body, body_digest (minimal stand-in included)
import build_index as BI      # noqa: E402  read_corpus, parse_frontmatter, body_of (minimal stand-in included)


# ------------------------------------------------------------------ manifest

def parse_manifest(path):
    """`## Claims` section of the draft: a list of `- claim:` blocks with indented `key: value`
    lines. Hand parsed, same as build_index does for frontmatter, so no yaml dependency.

        ## Claims
        - claim: "stprodshared01, stprodshared02 ..."
          evidence: 2026-09-03-storage-account-naming-proposal.md
          quote: "stprodshared01"
          topics: [storage, naming]
          type: figure
    """
    text = open(path, encoding="utf-8").read()
    m = re.search(r"^## Claims\s*$", text, re.M)
    if not m:
        return []
    section = text[m.end():]
    nxt = re.search(r"^## ", section, re.M)
    if nxt:
        section = section[:nxt.start()]
    claims, cur = [], None
    for raw in section.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("- "):
            cur = {}
            claims.append(cur)
            line = line[2:]
        elif cur is None:
            continue
        if ":" not in line:
            continue
        key, val = line.strip().split(":", 1)
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip() for v in val[1:-1].split(",") if v.strip()]
        elif len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        cur[key.strip()] = val
    return claims


def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


# ------------------------------------------------------------------ evidence

_corpus_cache = None


def corpus():
    """Every knowledge markdown file with its frontmatter, via build_index so the two agree."""
    global _corpus_cache
    if _corpus_cache is None:
        _corpus_cache = BI.read_corpus()
    return _corpus_cache


def vocabulary():
    return {t for r in corpus() for t in r["topics"]}


def resolve_evidence(ref):
    """Return (kind, text, date, label) for a knowledge file, a MON-N tracker item, or an inventory
    file. Date is the frontmatter date, the comment stamp, or '' when the source carries none."""
    m = re.fullmatch(r"EM-(\d+)", ref)
    if m:
        reg = json.load(open(os.path.join(HERE, "emails.json"), encoding="utf-8"))
        for e in reg.get("emails", []):
            if e.get("id") == ref:
                return ("register", json.dumps(e, indent=1, ensure_ascii=False), e.get("date", ""), ref)
        return None
    m = re.fullmatch(r"MON-(\d+)", ref)
    if m:
        if not os.path.isfile(TASK_PY):
            return None
        out = subprocess.run([sys.executable, TASK_PY, "show", TASK_REPO, m.group(1)],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None
        return ("tracker", out.stdout, "", ref)
    for base in (KNOWLEDGE, os.path.join(KNOWLEDGE, "inventory"), ENGAGEMENT):
        p = os.path.join(base, ref)
        if os.path.isfile(p):
            text = open(p, encoding="utf-8", errors="replace").read()
            meta = BI.parse_frontmatter(text) if p.endswith(".md") else {}
            date = meta.get("date", "") if isinstance(meta.get("date", ""), str) else ""
            if not date:
                dm = re.search(r"(\d{4}-\d{2}-\d{2})", ref)
                date = dm.group(1) if dm else ""
            kind = "inventory" if "/inventory/" in p else "knowledge"
            return (kind, text, date, os.path.relpath(p, ENGAGEMENT))
    return None


def quote_date_in_tracker(text, quote):
    """`task.py show` prints each note under a `--- YYYY-MM-DD` stamp. The evidence date for a quote
    found in a note is that note's stamp; a quote found in the description carries none."""
    stamp = ""
    for line in text.splitlines():
        m = re.match(r"^--- (\d{4}-\d{2}-\d{2})", line)
        if m:
            stamp = m.group(1)
        if norm(quote) in norm(line):
            return stamp
    return ""


def excerpt(text, quote, around=6):
    """The lines around the quote, so the judge can read the evidence without opening the file.
    Whole text when there is no quote or it is short enough to send entire."""
    lines = text.splitlines()
    if len(lines) <= 2 * around + 1 or not quote:
        return text[:6000]
    for i, ln in enumerate(lines):
        if norm(quote) in norm(ln):
            lo, hi = max(0, i - around), min(len(lines), i + around + 1)
            return "\n".join(lines[lo:hi])
    return ""


def file_text(name):
    p = os.path.join(KNOWLEDGE, name)
    return open(p, encoding="utf-8", errors="replace").read() if os.path.isfile(p) else ""


def newer_on_topics(topics, since, exclude=()):
    """Knowledge files sharing any topic and dated strictly after `since`. The staleness query.
    The draft under check is excluded: it is the thing being verified, not evidence for itself."""
    rows = [r for r in corpus() if r["file"] not in exclude and set(r["topics"]) & set(topics)]
    if not since:
        return sorted(r["file"] for r in rows)
    return sorted(r["file"] for r in rows if r["date"] and r["date"] > since)


# ------------------------------------------------------------------ values

VALUE_PATTERNS = [
    ("date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("date", re.compile(r"\b\d{1,2} (?:January|February|March|April|May|June|July|August|"
                        r"September|October|November|December)\b")),
    ("domain", re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+\.(?:com|net|org|win|local|io)\b")),
    ("identifier", re.compile(r"\b(?=[a-z0-9-]*\d)(?=[a-z0-9-]*[a-z])[a-z][a-z0-9-]{3,}\b")),
    ("number", re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w.])")),
    ("name", re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]{2,}(?: [A-Z][a-z]{2,})?\b")),
]
COMMON_WORDS = set("""hi thanks the and for are was were this that with from into your our his her
their whether what which when where would could should happy just still three two five one""".split())


def extract_values(body):
    seen, out = set(), []
    for kind, pat in VALUE_PATTERNS:
        for m in pat.finditer(body):
            v = m.group(0)
            key = (kind, v.lower())
            if key in seen or v.lower() in COMMON_WORDS:
                continue
            if kind == "number" and len(v.replace(",", "").replace(".", "")) < 2:
                continue
            seen.add(key)
            out.append({"kind": kind, "value": v})
    return out


def corpus_text():
    """One in memory pass over everything greppable. Knowledge, inventory, the email register and the
    open tracker. Around 150k tokens, trivial to hold, and it makes thirty lookups cost nothing."""
    blobs = {}
    for root, _, files in os.walk(KNOWLEDGE):
        for f in files:
            if f.endswith((".md", ".csv", ".json", ".txt", ".yaml", ".tf")):
                p = os.path.join(root, f)
                blobs[os.path.relpath(p, ENGAGEMENT)] = open(p, encoding="utf-8", errors="replace").read()
    p = os.path.join(HERE, "emails.json")
    if os.path.exists(p):
        blobs["emails/emails.json"] = open(p, encoding="utf-8").read()
    if os.path.isfile(TASK_PY):
        out = subprocess.run([sys.executable, TASK_PY, "list", TASK_REPO], capture_output=True, text=True)
        blobs["tracker (tracker list project)"] = out.stdout
    return blobs


def grep_values(values, blobs):
    for v in values:
        needle = v["value"].lower()
        hits = [name for name, text in blobs.items() if needle in text.lower()]
        v["found_in"] = len(hits)
        v["sample"] = hits[:3]
    return values


# ------------------------------------------------------------------ main

def run(draft, out_path):
    body = E.extract_body(draft)
    digest = E.body_digest(draft)
    manifest = parse_manifest(draft)
    vocab = vocabulary()
    problems = []
    body_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    covered = set()

    claims = []
    for i, c in enumerate(manifest, 1):
        rec = {"claim": c.get("claim", ""), "type": c.get("type", ""),
               "evidence": c.get("evidence", ""), "quote": c.get("quote", ""),
               "topics": c.get("topics", []) if isinstance(c.get("topics"), list) else []}
        if norm(rec["claim"]) not in norm(body):
            problems.append("claim %d is not in the body: %r" % (i, rec["claim"][:70]))
            rec["in_body"] = False
        else:
            rec["in_body"] = True
            for ln in body_lines:
                if norm(rec["claim"]) in norm(ln):
                    covered.add(ln)
        if rec["type"] == "platform":
            # No evidence, no staleness query, no topics. There is nothing in this workspace that
            # could confirm or refute how the platform behaves, and pretending otherwise is what produced
            # a claim pointing at a client's email as evidence for a platform constraint.
            if rec["evidence"] or rec["quote"] or rec["topics"]:
                problems.append("claim %d is type platform, so it must carry no evidence, "
                                "quote or topics" % i)
            rec.update({"evidence_status": "PLATFORM", "evidence_date": "",
                        "freshness": "N/A", "newer_candidates": [], "newer_summaries": {},
                        "auto": ""})
            claims.append(rec)
            continue

        bad = [t for t in rec["topics"] if t not in vocab]
        if bad:
            problems.append("claim %d uses topics not in INDEX.md vocabulary: %s" % (i, bad))
        if not rec["topics"]:
            problems.append("claim %d has no topics, so the staleness query cannot run" % i)

        ev = resolve_evidence(rec["evidence"]) if rec["evidence"] else None
        if not ev:
            rec["evidence_status"] = "MISSING"
            problems.append("claim %d evidence not found: %r" % (i, rec["evidence"]))
            rec["evidence_date"] = ""
        else:
            kind, text, date, label = ev
            rec["evidence_kind"] = kind
            rec["evidence"] = label
            rec["excerpt"] = excerpt(text, rec["quote"])
            if rec["quote"] and norm(rec["quote"]) in norm(text):
                rec["evidence_status"] = "QUOTE_OK"
                if kind == "tracker":
                    date = quote_date_in_tracker(text, rec["quote"]) or date
            elif rec["quote"]:
                rec["evidence_status"] = "QUOTE_MISSING"
                problems.append("claim %d quote not in %s: %r" % (i, label, rec["quote"][:60]))
            else:
                rec["evidence_status"] = "NO_QUOTE"
                problems.append("claim %d has no quote; the verifier must read the whole source" % i)
            rec["evidence_date"] = date
            if not date:
                problems.append("claim %d evidence carries no date; staleness cannot be bounded" % i)
        newer = (newer_on_topics(rec["topics"], rec.get("evidence_date", ""),
                                 exclude=(os.path.basename(draft),)) if rec["topics"] else [])
        rec["newer_candidates"] = newer
        # Summaries, not bodies. Inlining full bodies is what made the first version of this cost
        # more than the cold search it replaced: seven candidates at eight thousand characters each
        # is a bigger prompt than the whole corpus search ever was. The judge gets enough to see
        # whether a newer file plausibly touches the claim, and routes it onward when it might.
        by_file = {r["file"]: r for r in corpus()}
        # Summary line plus the opening of the body. The one line title alone was not enough to judge
        # relevance and the judge correctly routed claims to the expensive path rather than guess,
        # which happens on EVERY cascade run: /email-update writes a knowledge file about the thread,
        # and that file is then newer evidence on the same topics as the reply drafted beside it.
        # Full bodies are the other failure, they cost more than the cold search they replaced. Eight
        # hundred characters: four hundred was measurably too few, because a knowledge file opens with
        # provenance (thread, date, who was copied) and the judge reached the cap before the first
        # statement of fact, then judged the file from its title alone and blocked a claim the file
        # actually supported.
        rec["newer_summaries"] = {
            n: (by_file.get(n, {}).get("summary", "") + " || "
                + " ".join(BI.body_of(file_text(n)).split())[:800])
            for n in newer
        }
        rec["freshness"] = "FRESH" if (rec.get("evidence_date") and not newer) else "CHECK"
        claims.append(rec)

    # Deterministic verdicts. A claim whose quote was found in a dated direct observation, with
    # nothing newer on its topics, needs no model: the script has already established the quote is
    # there, the source is an observation, and no later evidence exists to contradict it. Judgement
    # is only needed where one of those is missing. This is the whole saving, and everything the
    # model still sees is genuinely undecidable without reading.
    values = grep_values(extract_values(body), corpus_text())
    OBSERVATION_KINDS = ("inventory", "register")
    for rec in claims:
        settled = (rec.get("evidence_status") == "QUOTE_OK"
                   and rec["freshness"] == "FRESH"
                   and rec["in_body"]
                   and rec.get("evidence_kind") in OBSERVATION_KINDS)
        rec["auto"] = "SUPPORTED" if settled else ""

    uncovered = [ln for ln in body_lines if ln not in covered]

    residue = [c for c in claims if not c["auto"]]
    result = {
        "draft": os.path.relpath(draft, ENGAGEMENT),
        "autoSettled": [c["claim"] for c in claims if c["auto"]],
        "needsJudgement": len(residue) + len([v for v in values if v["found_in"] == 0]) + len(uncovered),
        "bodySha256": digest,
        "claims": claims,
        "values": values,
        "uncovered_lines": uncovered,
        "problems": problems,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
        f.write("\n")

    # human summary
    print("precheck  %s" % result["draft"])
    print("  claims  %d in manifest" % len(claims))
    for c in claims:
        if c["type"] == "platform":
            print("    %-13s %-5s %s" % ("PLATFORM", "N/A", c["claim"][:60]))
            continue
        print("    %-13s %-5s %s  <- %s (%s)" % (
            c.get("evidence_status", "?"), c["freshness"], c["claim"][:60],
            c["evidence"], c.get("evidence_date") or "undated"))
        for n in c["newer_candidates"]:
            print("                  newer: %s" % n)
    missing = [v for v in values if v["found_in"] == 0]
    print("  values  %d extracted, %d not found anywhere" % (len(values), len(missing)))
    for v in missing:
        print("    NOT FOUND  %-10s %s" % (v["kind"], v["value"]))
    print("  uncovered lines  %d (the verifier classifies these; most are NOT_A_CLAIM)" % len(uncovered))
    if problems:
        print("  PROBLEMS")
        for p in problems:
            print("    " + p)
    print("  wrote  %s" % os.path.relpath(out_path, ENGAGEMENT))
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("draft")
    ap.add_argument("--out", help="default: emails/prechecks/<draft basename>.json")
    a = ap.parse_args()
    out = a.out or os.path.join(HERE, "prechecks",
                                os.path.splitext(os.path.basename(a.draft))[0] + ".json")
    sys.exit(run(a.draft, out))


if __name__ == "__main__":
    main()
