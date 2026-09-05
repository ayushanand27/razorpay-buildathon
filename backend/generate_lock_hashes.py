"""
One-off script -- fetches the published sha256 digest for each
package's Linux/cp313-compatible wheel(s) directly from PyPI's JSON
API (which already publishes these digests per file, so this needs no
downloads at all) and writes a requirements-lock.txt pip can install
with --require-hashes. Not part of the app itself; delete after use
or keep for regenerating the lock file when dependencies change.
"""
import json
import urllib.request

PACKAGES = [
    "annotated-types==0.8.0", "anyio==4.15.0", "certifi==2026.7.22",
    "charset-normalizer==3.5.1", "click==8.5.0", "fastapi==0.115.0",
    "greenlet==3.5.5", "h11==0.16.0", "httpcore==1.0.9", "httpx==0.27.2",
    "idna==3.19", "iniconfig==2.3.0", "packaging==26.3", "pluggy==1.6.0",
    "pydantic==2.13.5", "pydantic-core==2.46.5", "pytest==8.3.3",
    "python-dotenv==1.0.1", "requests==2.32.3", "sniffio==1.3.1",
    "SQLAlchemy==2.0.52", "sqlmodel==0.0.42", "starlette==0.38.6",
    "typing-inspection==0.4.4", "typing_extensions==4.16.0",
    "urllib3==2.7.0", "uvicorn==0.30.6",
]


def is_linux_compatible(filename: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    if "-py3-none-any" in filename or "-py2.py3-none-any" in filename:
        return True
    if "cp313" in filename and ("manylinux" in filename or "musllinux" in filename):
        return True
    return False


lines = []
for spec in PACKAGES:
    name, version = spec.split("==")
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.load(resp)
    hashes = []
    for f in data["urls"]:
        if is_linux_compatible(f["filename"]):
            hashes.append(f["digests"]["sha256"])
    if not hashes:
        raise SystemExit(f"No Linux-compatible wheel found for {spec} -- check manually")
    hash_lines = " \\\n".join(f"    --hash=sha256:{h}" for h in hashes)
    lines.append(f"{name}=={version} \\\n{hash_lines}")
    print(f"{spec}: {len(hashes)} hash(es)")

with open("requirements-lock.txt", "w") as f:
    f.write(
        "# Fully hash-locked (direct + transitive deps, exact versions,\n"
        "# sha256 digests published by PyPI for each file) -- generated via\n"
        "# generate_lock_hashes.py, which reads PyPI's own JSON API digests\n"
        "# directly (no download needed). Only covers the Linux/cp313\n"
        "# wheel(s) CI actually installs -- this is a single-target lock,\n"
        "# not a universal one, by design.\n"
        "#\n"
        "# Regenerate after changing requirements.txt with:\n"
        "#   python generate_lock_hashes.py\n\n"
    )
    f.write("\n".join(lines))
    f.write("\n")

print("\nWrote requirements-lock.txt")
