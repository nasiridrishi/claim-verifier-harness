"""Stand-in for the workspace email module: body is everything above the notes heading."""
import hashlib, sys

def extract_body(path):
    return open(path, encoding="utf-8").read().split("\n## Notes", 1)[0].strip() + "\n"

def body_digest(path):
    return hashlib.sha256(extract_body(path).encode()).hexdigest()

if __name__ == "__main__" and sys.argv[1] == "body":
    sys.stdout.write(extract_body(sys.argv[2]))
