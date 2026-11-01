"""Stand-in for the workspace index: frontmatter date, topics and summary per knowledge file."""
import os, re
HERE = os.path.dirname(os.path.abspath(__file__))

def parse_frontmatter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    meta = {}
    for line in (m.group(1).splitlines() if m else []):
        k, v = line.split(":", 1)
        v = v.strip()
        meta[k.strip()] = [t.strip() for t in v[1:-1].split(",")] if v.startswith("[") else v
    return meta

def body_of(text):
    return re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)

def read_corpus():
    rows = []
    for f in sorted(os.listdir(HERE)):
        if f.endswith(".md"):
            meta = parse_frontmatter(open(os.path.join(HERE, f), encoding="utf-8").read())
            rows.append({"file": f, "topics": meta.get("topics", []), "date": meta.get("date", ""),
                         "summary": meta.get("summary", "")})
    return rows
