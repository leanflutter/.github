#!/usr/bin/env python3
"""
Update README files by reading YAML data files and rendering them with Jinja2 templates.

Usage:
    python update_readme.py              # Update all README files
    python update_readme.py --dry-run    # Preview changes without writing
    python update_readme.py --no-fetch   # Skip external API calls (offline mode)
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from jinja2 import BaseLoader, Environment, StrictUndefined


PROFILE_DIR = Path(__file__).parent / "profile"

README_FILES = [
    PROFILE_DIR / "README.md",
]

MARKER_PREFIX = "<!--"
MARKER_SUFFIX = "-->"


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_file(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _http_get_json(url: str, token: str | None = None) -> dict:
    """Fetch JSON from a URL using urllib. Optionally pass a GitHub token."""
    headers = {"User-Agent": "update-readme-script"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            remaining = e.headers.get("X-RateLimit-Remaining", "?")
            reset = e.headers.get("X-RateLimit-Reset", "?")
            raise RuntimeError(
                f"HTTP 403 (rate limit exceeded, {remaining} remaining, "
                f"resets at {reset}). Set GITHUB_TOKEN env var or use --github-token."
            ) from e
        raise RuntimeError(f"HTTP {e.code}") from e


def _ensure_defaults(entity: dict) -> dict:
    """Ensure entity has all required fields with sensible defaults."""
    defaults = {
        "name": entity.get("github_id", ""),
        "image_url": "",
        "homepage": "",
        "description": "",
        "livepreview_url": "",
    }
    return {**defaults, **entity}


def fetch_github_user(github_id: str, token: str | None = None) -> dict:
    """Fetch GitHub user info. Returns dict with name, image_url, github_id."""
    url = f"https://api.github.com/users/{github_id}"
    try:
        data = _http_get_json(url, token=token)
        return {
            "github_id": github_id,
            "name": data.get("name") or data.get("login", github_id),
            "image_url": data.get("avatar_url", ""),
            "homepage": data.get("blog", ""),
        }
    except Exception as e:
        print(f"  [WARN] Failed to fetch GitHub user '{github_id}': {e}", file=sys.stderr)
        return {
            "github_id": github_id,
            "name": github_id,
            "image_url": "",
            "homepage": "",
        }


def enrich_sponsors(sponsors_data: dict, fetch: bool = True, token: str | None = None) -> dict:
    """Enrich sponsor entities with GitHub user info (or apply defaults)."""
    entities = sponsors_data.get("entities", [])
    enriched = []
    for entity in entities:
        github_id = entity["github_id"]
        if fetch:
            print(f"  Fetching GitHub user: {github_id}")
            info = fetch_github_user(github_id, token=token)
            merged = {**info, **entity}  # YAML values take precedence
        else:
            merged = dict(entity)
        enriched.append(_ensure_defaults(merged))
    return {"entities": enriched}


def enrich_packages(awesome_data: dict, fetch: bool = True, token: str | None = None) -> dict:
    """Enrich package entities with pub.dev info (or apply defaults)."""
    entities = awesome_data.get("entities", [])
    enriched = []
    for entity in entities:
        pub_id = entity.get("pub_id")
        github_id = entity["github_id"]
        name = pub_id or github_id.split("/")[-1]

        description = entity.get("description", "")
        if not description and fetch:
            if pub_id:
                print(f"  Fetching pub.dev package: {pub_id}")
                try:
                    url = f"https://pub.dev/api/packages/{pub_id}"
                    data = _http_get_json(url)
                    description = data.get("latest", {}).get("pubspec", {}).get("description", "")
                except Exception as e:
                    print(f"  [WARN] Failed to fetch pub.dev package '{pub_id}': {e}", file=sys.stderr)
            if not description:
                print(f"  Fetching GitHub repo: {github_id}")
                try:
                    url = f"https://api.github.com/repos/{github_id}"
                    data = _http_get_json(url, token=token)
                    description = data.get("description", "")
                except Exception as e:
                    print(f"  [WARN] Failed to fetch GitHub repo '{github_id}': {e}", file=sys.stderr)

        enriched.append(_ensure_defaults({
            **entity,
            "name": name,
            "description": description,
        }))
    return {
        **awesome_data,
        "entities": enriched,
    }


def render_template(template_str: str, data: dict) -> str:
    """Render a Jinja2 template string with the given data."""
    env = Environment(
        loader=BaseLoader(),
        undefined=StrictUndefined,
        trim_blocks=True,
    )
    # The templates use Liquid-style `blank` keyword; provide it as an empty string
    data.setdefault("blank", "")
    template = env.from_string(template_str)
    return template.render(**data)


def replace_section(content: str, marker: str, replacement: str) -> str:
    """Replace content between a pair of markers with the new content."""
    pattern = re.compile(
        rf"{re.escape(MARKER_PREFIX)}\s*{re.escape(marker)}\s*{re.escape(MARKER_SUFFIX)}"
        r".*?"
        rf"{re.escape(MARKER_PREFIX)}\s*{re.escape(marker)}\s*{re.escape(MARKER_SUFFIX)}",
        re.DOTALL,
    )
    replacement_block = (
        f"{MARKER_PREFIX} {marker} {MARKER_SUFFIX}\n"
        f"{replacement}\n"
        f"{MARKER_PREFIX} {marker} {MARKER_SUFFIX}"
    )
    if not pattern.search(content):
        print(f"  [WARN] Marker '{MARKER_PREFIX} {marker} {MARKER_SUFFIX}' not found, skipping", file=sys.stderr)
        return content
    return pattern.sub(replacement_block, content)


def main():
    parser = argparse.ArgumentParser(description="Update README from YAML data")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing files")
    parser.add_argument("--no-fetch", action="store_true", help="Skip fetching external APIs (use defaults)")
    args = parser.parse_args()

    do_fetch = not args.no_fetch

    # Step 1: Load data and templates
    print("[1/4] Loading YAML data and templates...")
    sponsors_data = load_yaml(PROFILE_DIR / "sponsors.yaml")
    projects_data = load_yaml(PROFILE_DIR / "projects.yaml")
    sponsors_tmpl = read_file(PROFILE_DIR / "sponsors.tmpl")
    projects_tmpl = read_file(PROFILE_DIR / "projects.tmpl")

    github_token = os.environ.get("GITHUB_TOKEN")

    # Step 2-3: Enrich data from external APIs
    if do_fetch:
        print("[2/4] Enriching sponsor data from GitHub API...")
        sponsors_data = enrich_sponsors(sponsors_data, fetch=True, token=github_token)
        print("[3/4] Enriching package data from pub.dev API...")
        projects_data = enrich_packages(projects_data, fetch=True, token=github_token)
    else:
        print("[2/4] Applying defaults (--no-fetch)...")
        sponsors_data = enrich_sponsors(sponsors_data, fetch=False)
        print("[3/4] Applying defaults (--no-fetch)...")
        projects_data = enrich_packages(projects_data, fetch=False)

    # Render templates
    sponsors_html = render_template(sponsors_tmpl, sponsors_data)
    projects_md = render_template(projects_tmpl, projects_data)

    # Step 4: Update README files
    print("[4/4] Updating README files...")
    for readme_path in README_FILES:
        if not readme_path.exists():
            print(f"  [SKIP] {readme_path} (not found)")
            continue

        print(f"  Processing: {readme_path}")
        content = read_file(readme_path)

        new_content = replace_section(content, "PROJECTS_MAKER", projects_md)
        new_content = replace_section(new_content, "SPONSORS_MAKER", sponsors_html)

        if new_content == content:
            print("    No changes")
        elif args.dry_run:
            print(f"    [DRY-RUN] Would update {readme_path}")
        else:
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            print("    Updated")

    print("\nDone!")


if __name__ == "__main__":
    main()
