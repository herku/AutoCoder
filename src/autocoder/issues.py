from __future__ import annotations

import hashlib
import heapq
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from autocoder.git import ensure_autocoder_ignored
from autocoder.prompts import load
from autocoder.types import Issue, Priority


PRIORITY_ORDER = [Priority.P0, Priority.P1, Priority.P2, Priority.P3]

ISSUE_BODY_MAX_CHARS = 4000


def fetch_issues_by_number(repo_path: str, numbers: list[int]) -> list[Issue]:
    """Fetch specific issues by number via gh issue view."""
    issues: list[Issue] = []
    for num in numbers:
        result = subprocess.run(
            [
                "gh", "issue", "view", str(num),
                "--json", "number,title,body,labels,url",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"  Warning: could not fetch issue #{num}: {result.stderr.strip()}", file=sys.stderr)
            continue
        raw = json.loads(result.stdout)
        issues.append(_parse_issue(raw, ""))
    return issues


def fetch_issue_comments(repo_path: str, number: int) -> list[str]:
    """Fetch an issue's comments as "author: body" strings.

    The discussion often carries clarifications and de-facto spec changes
    that never make it back into the issue body. Failures are non-fatal —
    returns [] so the pipeline proceeds with body-only context.
    """
    result = subprocess.run(
        ["gh", "issue", "view", str(number), "--json", "comments"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    comments = data.get("comments") or []
    out: list[str] = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        body = (c.get("body") or "").strip()
        if not body:
            continue
        author = (c.get("author") or {}).get("login", "unknown")
        out.append(f"{author}: {body}")
    return out


IMAGE_MAX_COUNT = 5
IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_DOWNLOAD_TIMEOUT = 30  # seconds per file

_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^\s)]+)")
_HTML_IMG_RE = re.compile(r"<img[^>]+src\s*=\s*[\"']?([^\"'\s>]+)", re.IGNORECASE)
_BARE_ATTACH_RE = re.compile(r"https?://github\.com/user-attachments/assets/[\w-]+")

# Only these hosts ever receive the gh token; URLs anywhere else are skipped
# entirely rather than fetched unauthenticated — issue bodies are untrusted
# input and shouldn't drive requests to arbitrary hosts.
_GITHUB_IMAGE_HOSTS = (
    "https://github.com/user-attachments/",
    "https://user-images.githubusercontent.com/",
    "https://private-user-images.githubusercontent.com/",
    "https://camo.githubusercontent.com/",
)

_CONTENT_TYPE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


def extract_image_urls(text: str) -> list[str]:
    """Extract GitHub-hosted image URLs from markdown/HTML issue text.

    Document order, deduped, capped at IMAGE_MAX_COUNT."""
    matches: list[tuple[int, str]] = []
    for regex in (_MD_IMAGE_RE, _HTML_IMG_RE, _BARE_ATTACH_RE):
        for m in regex.finditer(text):
            matches.append((m.start(), m.group(1) if m.groups() else m.group(0)))
    matches.sort(key=lambda t: t[0])
    seen: set[str] = set()
    urls: list[str] = []
    for _, url in matches:
        if url in seen or not url.startswith(_GITHUB_IMAGE_HOSTS):
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= IMAGE_MAX_COUNT:
            break
    return urls


def _gh_token(repo_path: str) -> str | None:
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            cwd=repo_path, capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def _ext_for_content_type(content_type: str) -> str | None:
    return _CONTENT_TYPE_EXT.get(content_type.split(";")[0].strip().lower())


def _download_image(url: str, dest_base: Path, token: str | None) -> Path | None:
    """Download one image to dest_base, rename with the content-type extension.

    Returns the final path, or None on any failure (never raises)."""
    cmd = [
        "curl", "-fsSL",
        "--max-redirs", "5",
        "--max-time", str(IMAGE_DOWNLOAD_TIMEOUT),
        "--max-filesize", str(IMAGE_MAX_BYTES),
        "-o", str(dest_base),
        "-w", "%{content_type}",
    ]
    if token:
        # Attachment URLs 302-redirect to signed S3 URLs that reject requests
        # carrying an Authorization header on top of the signature. curl >=
        # 7.58 strips custom Authorization headers on cross-host redirects,
        # so the token only ever reaches the GitHub host.
        cmd += ["-H", f"Authorization: token {token}"]
    cmd.append(url)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            timeout=IMAGE_DOWNLOAD_TIMEOUT + 10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        if result.returncode != 0:
            dest_base.unlink(missing_ok=True)
            return None
        ext = _ext_for_content_type(result.stdout.strip())
        if ext is None:  # HTML error page or other non-image response
            dest_base.unlink(missing_ok=True)
            return None
        # --max-filesize is ineffective on chunked responses; re-check.
        size = dest_base.stat().st_size
        if size == 0 or size > IMAGE_MAX_BYTES:
            dest_base.unlink(missing_ok=True)
            return None
        dest = dest_base.with_suffix(ext)
        dest_base.rename(dest)
        return dest
    except OSError:
        return None


def download_issue_images(repo_path: str, issue_number: int, texts: list[str]) -> list[str]:
    """Download GitHub-hosted images referenced in issue body/comments into
    {repo}/.autocoder/images/issue-<N>/.

    Screenshots and mockups often constrain the implementation, but fetching
    them needs GitHub auth the sandboxed agent doesn't have — so the
    orchestrator downloads them and the prompt points the agent's Read tool
    at the local files. Best-effort: every failure is swallowed. Returns
    repo-RELATIVE paths (docker mode mounts the repo at /workspace, so
    absolute host paths would dangle) of the files that downloaded OK.
    """
    urls = extract_image_urls("\n\n".join(texts))
    if not urls:
        return []
    ensure_autocoder_ignored(repo_path)
    rel_dir = Path(".autocoder") / "images" / f"issue-{issue_number}"
    dest_dir = Path(repo_path) / rel_dir
    shutil.rmtree(dest_dir, ignore_errors=True)  # idempotent across reruns
    try:
        dest_dir.mkdir(parents=True)
    except OSError:
        return []
    token = _gh_token(repo_path)
    paths: list[str] = []
    for i, url in enumerate(urls, 1):
        dest = _download_image(url, dest_dir / f"image-{i}", token)
        if dest is not None:
            paths.append(str(rel_dir / dest.name))
    return paths


def fetch_issues(repo_path: str, labels: list[str], limit: int = 0) -> list[Issue]:
    """Fetch open issues. If limit > 0, cap the result count."""
    all_issues: list[Issue] = []
    seen: set[int] = set()

    if not labels:
        raw = _gh_fetch_all(repo_path)
        for item in raw:
            if item["number"] not in seen:
                seen.add(item["number"])
                all_issues.append(_parse_issue(item, ""))
    else:
        for label in labels:
            raw = _gh_fetch(repo_path, label)
            for item in raw:
                if item["number"] not in seen:
                    seen.add(item["number"])
                    all_issues.append(_parse_issue(item, label))

    all_issues = _priority_sort(all_issues)
    return all_issues[:limit] if limit > 0 else all_issues


def _gh_fetch_all(repo_path: str) -> list[dict]:
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--state", "open",
            "--json", "number,title,body,labels,url",
            "--limit", "500",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else []


def _gh_fetch(repo_path: str, label: str) -> list[dict]:
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--label", label,
            "--state", "open",
            "--json", "number,title,body,labels,url",
            "--limit", "500",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else []


def _parse_issue(raw: dict, default_priority: str) -> Issue:
    label_names = [l["name"] for l in raw.get("labels", [])]
    priority = _extract_priority(label_names, default_priority)
    return Issue(
        number=raw["number"],
        title=raw["title"],
        body=raw.get("body", "") or "",
        labels=label_names,
        priority=priority,
        url=raw.get("url", ""),
    )


def parse_sub_issues(body: str) -> list[int]:
    """Extract issue numbers referenced in the epic body.

    Matches:
      - [ ] #123 description
      - [x] #123 description
      - #123 description (bare list item)
      - [ ] https://github.com/org/repo/issues/123
    """
    if not body:
        return []
    # Checkbox items: - [ ] #N or - [x] #N (with optional URL form)
    checkbox_pat = re.compile(
        r"^[-*]\s*\[[ xX]\]\s*(?:https?://github\.com/[^/]+/[^/]+/issues/)?#?(\d+)",
        re.MULTILINE,
    )
    # Bare list items: - #N
    bare_pat = re.compile(r"^[-*]\s+#(\d+)", re.MULTILINE)

    seen: set[int] = set()
    result: list[int] = []
    for pat in (checkbox_pat, bare_pat):
        for m in pat.finditer(body):
            num = int(m.group(1))
            if num not in seen:
                seen.add(num)
                result.append(num)
    return result


def fetch_sub_issues(repo_path: str, numbers: list[int]) -> tuple[list[Issue], list[int]]:
    """Fetch sub-issues, returning (open_issues, closed_numbers)."""
    open_issues: list[Issue] = []
    closed: list[int] = []
    for num in numbers:
        result = subprocess.run(
            [
                "gh", "issue", "view", str(num),
                "--json", "number,title,body,labels,url,state",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"  Warning: could not fetch sub-issue #{num}: {result.stderr.strip()}", file=sys.stderr)
            continue
        raw = json.loads(result.stdout)
        if raw.get("state", "").upper() == "CLOSED":
            closed.append(num)
        else:
            open_issues.append(_parse_issue(raw, ""))
    return open_issues, closed


def _extract_priority(labels: list[str], default: str) -> Priority:
    for p in PRIORITY_ORDER:
        if p.value in labels:
            return p
    try:
        return Priority(default)
    except ValueError:
        return Priority.P3


def _priority_sort(issues: list[Issue]) -> list[Issue]:
    order = {p: i for i, p in enumerate(PRIORITY_ORDER)}
    return sorted(issues, key=lambda iss: (order.get(iss.priority, 99), iss.number))


def _dependency_reorder(
    issues: list[Issue],
    dependencies: dict[int, list[int]],
) -> list[Issue]:
    """Topological sort respecting dependencies while preserving priority order."""
    if not dependencies:
        return issues

    issue_map = {iss.number: iss for iss in issues}
    present = set(issue_map)
    order = {p: i for i, p in enumerate(PRIORITY_ORDER)}

    # Build in-degree map (only for edges where both ends are present)
    in_degree: dict[int, int] = {n: 0 for n in present}
    # reverse_adj: blocker -> list of issues it unblocks
    reverse_adj: dict[int, list[int]] = {n: [] for n in present}

    for num, blockers in dependencies.items():
        if num not in present:
            continue
        for b in blockers:
            if b in present:
                in_degree[num] += 1
                reverse_adj[b].append(num)

    # Kahn's algorithm with priority heap
    heap: list[tuple[int, int, int]] = []
    for num in present:
        if in_degree[num] == 0:
            iss = issue_map[num]
            heapq.heappush(heap, (order.get(iss.priority, 99), iss.number, num))

    result: list[Issue] = []
    while heap:
        _, _, num = heapq.heappop(heap)
        result.append(issue_map[num])
        for dependent in reverse_adj[num]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                iss = issue_map[dependent]
                heapq.heappush(heap, (order.get(iss.priority, 99), iss.number, dependent))

    # Handle cycles: remaining nodes with in_degree > 0
    if len(result) < len(issues):
        remaining = [n for n in present if n not in {iss.number for iss in result}]
        remaining_issues = sorted(
            [issue_map[n] for n in remaining],
            key=lambda iss: (order.get(iss.priority, 99), iss.number),
        )
        nums = ", ".join(f"#{n}" for n in remaining)
        print(f"  Warning: dependency cycle detected involving issues {nums}. Breaking cycle.", file=sys.stderr)
        result.extend(remaining_issues)

    return result


# ---------------------------------------------------------------------------
# Prioritization cache
# ---------------------------------------------------------------------------

_CACHE_DIR = ".autocoder"
_CACHE_FILE = "prioritization_cache.json"


def _load_cache(
    repo_path: str, issues: list[Issue],
) -> tuple[dict[int, Priority], dict[int, str], dict[int, list[int]]] | None:
    """Load cached prioritization results.

    Returns cached priorities/reasons/deps for issues that exist in the cache,
    filtered to current issue numbers. Returns None only if no cache file exists
    or the file is corrupt.
    """
    cache_path = Path(repo_path) / _CACHE_DIR / _CACHE_FILE
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    try:
        all_priorities = {int(k): Priority(v) for k, v in data["priorities"].items()}
        all_reasons = {int(k): v for k, v in data["reasons"].items()}
        all_deps = {int(k): v for k, v in data["dependencies"].items()}
    except (KeyError, ValueError):
        return None
    current_numbers = {iss.number for iss in issues}
    # Filter to current issues only, strip stale dependency refs
    priorities = {k: v for k, v in all_priorities.items() if k in current_numbers}
    reasons = {k: v for k, v in all_reasons.items() if k in current_numbers}
    dependencies = {
        k: [d for d in v if d in current_numbers]
        for k, v in all_deps.items()
        if k in current_numbers
    }
    if not priorities:
        return None
    return priorities, reasons, dependencies


def _save_cache(
    repo_path: str,
    issues: list[Issue],
    priorities: dict[int, Priority],
    reasons: dict[int, str],
    dependencies: dict[int, list[int]],
) -> None:
    """Save prioritization results to cache."""
    try:
        cache_dir = Path(repo_path) / _CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / _CACHE_FILE
        data = {
            "cached_numbers": sorted(priorities.keys()),
            "priorities": {str(k): v.value for k, v in priorities.items()},
            "reasons": {str(k): v for k, v in reasons.items()},
            "dependencies": {str(k): v for k, v in dependencies.items()},
        }
        cache_path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def truncate_body(body: str, max_chars: int = ISSUE_BODY_MAX_CHARS) -> str:
    """Truncate body preserving paragraph boundaries."""
    if len(body) <= max_chars:
        return body
    # Cut at last paragraph break before limit
    truncated = body[:max_chars]
    last_break = truncated.rfind("\n\n")
    if last_break > max_chars // 2:
        truncated = truncated[:last_break]
    return truncated + "\n\n[...truncated]"


# ---------------------------------------------------------------------------
# Auto-prioritization via claude -p
# ---------------------------------------------------------------------------

PRIORITIZE_BODY_MAX = 1500
PROMPT_CHAR_LIMIT = 400_000


def analyze_and_prioritize(
    issues: list[Issue],
    repo_path: str,
    triage_model: str = "sonnet",
    force: bool = False,
) -> tuple[list[Issue], dict[int, str], dict[int, list[int]]]:
    """Send all issues to Claude for AI-based priority scoring.

    Uses partial caching: cached issues keep their priorities, only new
    (uncached) issues are sent to the AI. Results are merged and saved.
    """
    if not issues:
        return issues, {}, {}

    priorities: dict[int, Priority] = {}
    reasons: dict[int, str] = {}
    dependencies: dict[int, list[int]] = {}
    issues_to_prioritize = issues

    if not force:
        cached = _load_cache(repo_path, issues)
        if cached is not None:
            priorities, reasons, dependencies = cached
            cached_numbers = set(priorities.keys())
            uncached = [i for i in issues if i.number not in cached_numbers]
            if not uncached:
                for issue in issues:
                    if issue.number in priorities:
                        issue.priority = priorities[issue.number]
                sorted_issues = _priority_sort(issues)
                sorted_issues = _dependency_reorder(sorted_issues, dependencies)
                print("  Using cached prioritization results.")
                return sorted_issues, reasons, dependencies
            print(f"  {len(cached_numbers)} issues cached, {len(uncached)} new issues to prioritize...")
            issues_to_prioritize = uncached

    prompt = _build_prioritize_prompt(issues_to_prioritize, repo_path)

    result = subprocess.run(
        ["claude", "-p", "--model", triage_model, "--output-format", "text"],
        input=prompt,
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    if result.returncode != 0:
        print(f"  Warning: auto-prioritize failed ({result.returncode}), using default priorities", file=sys.stderr)
        # Still apply any cached priorities we have
        for issue in issues:
            if issue.number in priorities:
                issue.priority = priorities[issue.number]
        return _priority_sort(issues), reasons, dependencies

    new_priorities, new_reasons, new_dependencies = _parse_priority_response(
        result.stdout, issues_to_prioritize,
    )

    if not new_priorities and not priorities:
        return _priority_sort(issues), {}, {}

    # Merge new results into cached
    priorities.update(new_priorities)
    reasons.update(new_reasons)
    dependencies.update(new_dependencies)

    for issue in issues:
        if issue.number in priorities:
            issue.priority = priorities[issue.number]

    sorted_issues = _priority_sort(issues)
    sorted_issues = _dependency_reorder(sorted_issues, dependencies)
    _save_cache(repo_path, issues, priorities, reasons, dependencies)
    return sorted_issues, reasons, dependencies


def _build_prioritize_prompt(issues: list[Issue], repo_path: str = "") -> str:
    body_max = PRIORITIZE_BODY_MAX

    while body_max >= 200:
        formatted = _format_issues_for_prompt(issues, body_max)
        prompt = load("prioritize", repo_path or None).format(formatted_issues=formatted)
        if len(prompt) <= PROMPT_CHAR_LIMIT:
            return prompt
        body_max //= 2

    formatted = _format_issues_for_prompt(issues, 200)
    return load("prioritize", repo_path or None).format(formatted_issues=formatted)


def _format_issues_for_prompt(issues: list[Issue], body_max: int) -> str:
    parts = []
    for iss in issues:
        body = iss.body[:body_max] if len(iss.body) > body_max else iss.body
        labels_str = ", ".join(iss.labels) if iss.labels else "(none)"
        parts.append(
            f"### Issue #{iss.number}: {iss.title}\n"
            f"Labels: {labels_str}\n"
            f"Body:\n{body}"
        )
    return "\n\n".join(parts)


def _parse_priority_response(
    raw: str, issues: list[Issue]
) -> tuple[dict[int, Priority], dict[int, str], dict[int, list[int]]]:
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        print("  Warning: could not parse auto-prioritize response as JSON", file=sys.stderr)
        return {}, {}, {}

    if not isinstance(data, list):
        return {}, {}, {}

    valid_numbers = {iss.number for iss in issues}
    priorities: dict[int, Priority] = {}
    reasons: dict[int, str] = {}
    dependencies: dict[int, list[int]] = {}

    for entry in data:
        if not isinstance(entry, dict):
            continue
        num = entry.get("number")
        pri = entry.get("priority", "")
        reason = entry.get("reason", "")
        blocked_by = entry.get("blocked_by", [])

        if num not in valid_numbers:
            continue
        try:
            priorities[num] = Priority(pri)
        except ValueError:
            continue
        if reason:
            reasons[num] = reason
        if isinstance(blocked_by, list):
            valid_blockers = [b for b in blocked_by if isinstance(b, int) and b in valid_numbers]
            if valid_blockers:
                dependencies[num] = valid_blockers

    return priorities, reasons, dependencies


