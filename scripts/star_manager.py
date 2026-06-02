#!/usr/bin/env python3
"""
star_manager.py - Incremental star classification and GitHub Lists update.
Runs in GitHub Actions on weekly schedule or manual trigger.

Flow:
  1. Load rules + state
  2. Fetch all starred repos from GitHub (GraphQL paginated)
  3. Identify new repos since last run
  4. Classify new repos using keyword rules
  5. Push new repos to GitHub Lists
  6. Save updated state
"""
import json, os, sys, subprocess, time, re
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "state.json")
RULES_FILE = os.path.join(SCRIPT_DIR, "rules.json")
GH = ["gh", "api", "graphql"]
BATCH_SIZE = 10

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def run_graphql(query, label=""):
    """Run a GraphQL query/mutation."""
    try:
        r = subprocess.run(
            GH + ["-f", f"query={query}"],
            capture_output=True, text=True, timeout=120
        )
        if r.returncode != 0:
            log(f"gh error: {r.stderr[:200]}")
            return None
        d = json.loads(r.stdout)
        if "errors" in d:
            log(f"GraphQL errors: {d['errors']}")
            return None
        return d
    except Exception as e:
        log(f"Exception: {e}")
        return None

# ── Load rules ──
with open(RULES_FILE) as f:
    config = json.load(f)

RULES = config["rules"]
PRIORITY = config["priority"]
CROSS_RULES = config["cross_rules"]
OVERRIDES = config["overrides"]
CN_KEYWORDS = config.get("cn_keywords", {})

def compile_patterns(cat):
    """Compile regex patterns for a category, return list of compiled or None."""
    return [re.compile(p, re.IGNORECASE) for p in RULES.get(cat, [])]

# Pre-compile all patterns
COMPILED = {cat: compile_patterns(cat) for cat in PRIORITY}

def classify(repo_name, description="", topics=""):
    """
    Classify a repo into 1-3 categories using keyword rules.
    Returns sorted list of categories.
    """
    combined = f"{repo_name} {description} {topics}".lower()
    matched = []

    # Check overrides first
    for key, cats_str in OVERRIDES.items():
        if key.lower() in combined:
            return cats_str.split(",")

    # Phase 1: Primary classification
    for cat in PRIORITY:
        patterns = COMPILED.get(cat, [])
        for pat in patterns:
            if pat.search(combined):
                if cat not in matched:
                    matched.append(cat)
                break
        if len(matched) >= 3:
            break

    # Phase 2: Cross-category rules
    if matched:
        for trigger_str, cross_pat_str, target_cat in CROSS_RULES:
            if target_cat in matched:
                continue
            # Check if any trigger category matches
            if trigger_str:
                triggers = [t.strip() for t in trigger_str.split(",")]
                if not any(t in matched for t in triggers):
                    continue
            # Check cross pattern
            if cross_pat_str:
                if re.search(cross_pat_str, combined, re.IGNORECASE):
                    if target_cat not in matched:
                        matched.append(target_cat)
            else:
                # No pattern = always add (e.g. MCP → AI开发工具)
                if target_cat not in matched:
                    matched.append(target_cat)

    # Phase 3: Chinese keyword fallback
    if not matched:
        cn = combined
        for cat, kws in CN_KEYWORDS.items():
            if any(kw in cn for kw in kws):
                if cat not in matched:
                    matched.append(cat)
                break

    # Fallback
    if not matched:
        matched.append("其他领域")

    return matched[:3]


def get_starred_repos(after=None, accumulated=None):
    """Fetch all starred repos with pagination via GraphQL.

    Raises RuntimeError if any page fails so callers never save state from a
    partial fetch.
    """
    if accumulated is None:
        accumulated = []

    after_arg = f', after: "{after}"' if after else ""
    query = f"""
    {{
      viewer {{
        starredRepositories(first: 100, orderBy: {{field: STARRED_AT, direction: DESC}}{after_arg}) {{
          totalCount
          pageInfo {{ hasNextPage endCursor }}
          edges {{
            starredAt
            node {{
              id
              nameWithOwner
              description
              primaryLanguage {{ name }}
              repositoryTopics(first: 10) {{
                nodes {{ topic {{ name }} }}
              }}
            }}
          }}
        }}
      }}
    }}
    """
    result = run_graphql(query)
    if not result:
        raise RuntimeError(f"Failed to fetch starred repositories page after={after!r}")

    starred = result["data"]["viewer"]["starredRepositories"]
    total = starred["totalCount"]
    page_info = starred["pageInfo"]
    edges = starred["edges"]

    for edge in edges:
        repo = edge["node"]
        topics = [t["topic"]["name"] for t in repo.get("repositoryTopics", {}).get("nodes", [])]
        accumulated.append({
            "id": repo["id"],
            "name": repo["nameWithOwner"],
            "description": repo.get("description") or "",
            "language": repo["primaryLanguage"]["name"] if repo.get("primaryLanguage") else "",
            "topics": topics,
            "starred_at": edge["starredAt"],
        })

    if page_info["hasNextPage"]:
        return get_starred_repos(page_info["endCursor"], accumulated)

    return accumulated, page_info, total


def load_state():
    """Load state file or return defaults."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_run": None, "last_starred_at": None, "processed_repos": 0}


def save_state(state):
    """Save state file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log(f"State saved: {state['processed_repos']} repos processed")


def get_list_id_map():
    """Fetch current GitHub lists and return {name: id} mapping."""
    query = "{ viewer { lists(first: 50) { nodes { id name } } } }"
    result = run_graphql(query)
    if not result:
        return {}
    lists = result["data"]["viewer"]["lists"]["nodes"]
    return {l["name"]: l["id"] for l in lists}


def push_to_lists(repos, list_map):
    """Assign classified repos to their GitHub Lists via batched mutations."""
    total = len(repos)
    log(f"Pushing {total} repos to GitHub Lists...")

    ok = err = 0
    for i in range(0, total, BATCH_SIZE):
        batch = repos[i:i + BATCH_SIZE]
        parts = []
        for j, repo in enumerate(batch):
            alias = f"x{i+j}"
            lids = [list_map[c] for c in repo["categories"] if c in list_map]
            if not lids:
                continue
            lids_str = ", ".join(f'"{lid}"' for lid in lids)
            parts.append(
                f'{alias}: updateUserListsForItem(input: {{'
                f'itemId: "{repo["id"]}", listIds: [{lids_str}]'
                f'}}) {{ clientMutationId }}'
            )

        if not parts:
            continue

        mutation_str = f"mutation {{ {' '.join(parts)} }}"
        result = run_graphql(mutation_str)
        if result and "data" in result and "errors" not in result:
            ok += len(batch)
        else:
            err += len(batch)

        time.sleep(0.3)

        if (i // BATCH_SIZE) % 5 == 0:
            log(f"  Progress: {min(i+BATCH_SIZE, total)}/{total} (ok={ok}, err={err})")

    log(f"Push complete: {ok} OK, {err} errors")
    return ok, err


def main():
    log("=" * 50)
    log("Star Manager - Incremental Star Classification")
    log("=" * 50)

    # Force mode from env (manual trigger)
    force = os.environ.get("FORCE_RECLASSIFY", "").lower() == "true"
    if force:
        log("FORCE mode: reclassifying all repos")

    # Load state
    state = load_state()
    log(f"State: last_run={state['last_run']}, "
         f"last_starred_at={state['last_starred_at']}, "
         f"processed={state['processed_repos']}")

    # Fetch all starred repos
    log("Fetching starred repos from GitHub...")
    all_repos, _, total = get_starred_repos()
    log(f"Fetched {len(all_repos)}/{total} repos")
    if len(all_repos) != total:
        raise RuntimeError(f"Incomplete starred repository fetch: got {len(all_repos)} of {total}")

    # Identify new repos
    last_starred = state.get("last_starred_at")
    if force or not last_starred:
        new_repos = all_repos
        log(f"Processing ALL {len(new_repos)} repos")
    else:
        new_repos = [r for r in all_repos if r["starred_at"] > last_starred]
        log(f"New repos since {last_starred}: {len(new_repos)}")

    if not new_repos and not force:
        log("No new repos. Nothing to do.")
        return

    # Classify new repos
    log("Classifying repos...")
    repo_id_map = {}
    for repo in all_repos:
        repo_id_map[repo["name"]] = repo["id"]

    classified = []
    for repo in new_repos:
        topics_str = " ".join(repo["topics"])
        cats = classify(repo["name"], repo["description"], topics_str)
        repo["categories"] = cats
        classified.append(repo)

    # Stats
    cat_counts = {}
    unlabeled = 0
    for repo in classified:
        for c in repo["categories"]:
            cat_counts[c] = cat_counts.get(c, 0) + 1
        if repo["categories"] == ["其他领域"]:
            unlabeled += 1

    log(f"Classified {len(classified)} repos:")
    for cat in sorted(cat_counts, key=lambda c: -cat_counts[c]):
        log(f"  {cat_counts[cat]:>4}  {cat}")
    log(f"  唯一标签 (其他领域): {unlabeled}")
    log(f"  多标签率: {sum(v for v in cat_counts.values()) / max(len(classified), 1):.1f}x")

    # If force mode, we need to push ALL repos (not just new) to overwrite
    if force:
        # Re-classify ALL repos
        log("Force mode: reclassifying ALL repos...")
        all_classified = []
        for repo in all_repos:
            topics_str = " ".join(repo["topics"])
            repo["categories"] = classify(repo["name"], repo["description"], topics_str)
            all_classified.append(repo)
        to_push = all_classified
    else:
        # Only push new repos (they'll be added to lists alongside existing assignments)
        to_push = classified

    # Push to GitHub Lists
    list_map = get_list_id_map()
    log(f"Found {len(list_map)} GitHub Lists")

    missing_lists = set()
    for repo in to_push:
        for c in repo["categories"]:
            if c not in list_map:
                missing_lists.add(c)
    if missing_lists:
        log(f"MISSING LISTS: {missing_lists}")
        log("Create these lists manually or add them to rules.json")
        # Remove repos with missing categories
        to_push = [r for r in to_push if not missing_lists.intersection(r["categories"])]

    ok, err = push_to_lists(to_push, list_map)

    # Update state
    max_starred = max(r["starred_at"] for r in all_repos)
    new_state = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "last_starred_at": max_starred,
        "processed_repos": len(all_repos),
        "last_push": {
            "total": len(to_push),
            "ok": ok,
            "err": err,
        }
    }
    save_state(new_state)

    log("Done!")


if __name__ == "__main__":
    main()
