"""Turn unittest failures into GitHub Actions annotations (visible without access to the job log)."""
import re
import sys


def main(path):
    text = open(path, encoding="utf-8", errors="replace").read()
    blocks = re.split(r"\n={20,}\n", text)
    n = 0
    for b in blocks:
        m = re.match(r"(FAIL|ERROR): (\S+) \(([^)]+)\)", b)
        if not m:
            continue
        body = b.split("-" * 70, 1)[-1].split("\n" + "-" * 70 + "\nRan ", 1)[0]
        lines = [x for x in body.strip().splitlines() if x.strip()]
        where = next((x.strip() for x in reversed(lines) if x.strip().startswith('File "')), "")
        msg = " | ".join(x.strip() for x in lines[-4:])
        msg = (where + " | " + msg).replace("%", "%25").replace("\r", "").replace("\n", " ")[:900]
        print(f"::error title={m.group(1)} {m.group(3)}::{msg}")
        n += 1
        if n >= 10:
            break
    if not n:
        print("::error title=tests::" + " | ".join(text.strip().splitlines()[-6:])[:900])


if __name__ == "__main__":
    main(sys.argv[1])
