#!/usr/bin/env python3
"""Categorize GitHub stars and organize them into lists (private by default)."""

import argparse
import csv
import json
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_FILES = (
  "category-plan.json",
  "category-descriptions.json",
  "list-visibility.json",
  "starred-repos.csv",
  "categorized-stars.txt",
  "categorized-stars.csv",
  "categorization-audit.json",
  "starred-metadata.json",
  "uncategorized-repos.json",
  "github-lists-after.json",
  "last-sync.json",
)


def data_path(name):
  """Locate account files inside the ignored data directory."""
  return DATA_DIR / name


class OrganizerError(Exception):
  """An expected setup, validation, or GitHub API error."""


# File and command-line helpers


def unique_json_object(pairs):
  """Reject duplicate JSON keys instead of silently discarding assignments."""
  result = {}
  for key, value in pairs:
    if key in result:
      raise ValueError(f"Duplicate JSON key: {key!r}")
    result[key] = value
  return result


def load_json(path):
  """Read user-editable UTF-8 JSON with an actionable file-specific error."""
  try:
    return json.loads(
      path.read_text(encoding="utf-8-sig"),
      object_pairs_hook=unique_json_object,
    )
  except FileNotFoundError as exc:
    raise OrganizerError(
      f"Missing {path}. Run 'python3 github_stars.py init' for a new setup. "
      "For an existing setup, restore the missing file from your backup."
    ) from exc
  except (OSError, ValueError) as exc:
    raise OrganizerError(f"Cannot read {path}: {exc}") from exc


def save_json(path, value):
  """Replace a JSON file atomically after writing its complete contents."""
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  temp = path.with_name(path.name + ".tmp")
  temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
  temp.replace(path)


def gh(*args, payload=None):
  """Call GitHub.com through the user's authenticated GitHub CLI."""
  try:
    result = subprocess.run(
      ["gh", *args, "--hostname", "github.com"],
      capture_output=True,
      text=True,
      encoding="utf-8",
      input=json.dumps(payload) if payload is not None else None,
    )
  except FileNotFoundError as exc:
    raise OrganizerError("Install GitHub CLI (gh), then run gh auth login.") from exc
  if result.returncode:
    message = result.stderr.strip() or result.stdout.strip()
    if "required scopes" in message or "INSUFFICIENT_SCOPES" in message:
      message += "\nAuthorize list editing: gh auth refresh -h github.com -s user"
    raise OrganizerError(message)
  return result.stdout


def graphql(query, variables=None):
  response = gh(
    "api", "graphql", "--input", "-",
    payload={"query": query, "variables": variables or {}},
  )
  result = json.loads(response)
  if result.get("errors"):
    raise OrganizerError(json.dumps(result["errors"]))
  return result["data"]


# GitHub reads


def decode_stream(text):
  """Read concatenated JSON pages, including output from older gh versions."""
  decoder = json.JSONDecoder()
  records = []
  while text.strip():
    text = text.lstrip()
    page, offset = decoder.raw_decode(text)
    if not isinstance(page, list):
      raise OrganizerError("Expected an array from the GitHub stars endpoint.")
    records.extend(page)
    text = text[offset:]
  return records


def fetch_stars():
  return decode_stream(gh("api", "--paginate", "user/starred?per_page=100"))


def fetch_list_page(cursor):
  """Fetch one page of the authenticated user's lists."""
  query = """
    query($cursor: String) {
      viewer {
        login
        lists(first: 100, after: $cursor) {
          nodes {
            id
            name
            description
            isPrivate
            items(first: 100) {
              totalCount
              nodes { __typename ... on Repository { nameWithOwner id } }
              pageInfo { hasNextPage endCursor }
            }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
  return graphql(query, {"cursor": cursor})["viewer"]


def fetch_list_items_page(list_id, cursor):
  """Fetch one additional page of repositories from a list."""
  query = """
    query($id: ID!, $cursor: String) {
      node(id: $id) {
        ... on UserList {
          items(first: 100, after: $cursor) {
            totalCount
            nodes { __typename ... on Repository { nameWithOwner id } }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
    """
  variables = {"id": list_id, "cursor": cursor}
  return graphql(query, variables)["node"]["items"]


def fetch_all_list_items(entry):
  """Load every repository assigned to one list."""
  items = entry["items"]
  while items["pageInfo"]["hasNextPage"]:
    cursor = items["pageInfo"]["endCursor"]
    page = fetch_list_items_page(entry["id"], cursor)
    items["nodes"].extend(page["nodes"])
    items["pageInfo"] = page["pageInfo"]
    items["totalCount"] = page["totalCount"]

  if len(items["nodes"]) != items["totalCount"]:
    raise OrganizerError("List changed while reading it; retry: " + entry["name"])
  return items


def fetch_lists():
  """Fetch all lists and all repository memberships for the current user."""
  lists = []
  cursor = None
  login = None
  while True:
    data = fetch_list_page(cursor)
    login = data["login"]
    lists.extend(data["lists"]["nodes"])
    page = data["lists"]["pageInfo"]
    if not page["hasNextPage"]:
      break
    cursor = page["endCursor"]
  for entry in lists:
    fetch_all_list_items(entry)
  return {"login": login, "lists": lists}


# Local plan validation and rendering


def validate_repository_name(name):
  parts = name.split("/")
  if len(parts) != 2 or not all(parts) or any(character.isspace() for character in name):
    raise OrganizerError("Use owner/repository names without whitespace: " + name)


def validate_plan(plan, expected=None):
  if not isinstance(plan, dict) or not plan:
    raise OrganizerError("The plan must contain at least one nonempty category.")
  names = []
  category_names = []
  for category, items in plan.items():
    if not isinstance(category, str) or not category.strip():
      raise OrganizerError(f"Category names must be nonempty strings: {category!r}")
    if category != category.strip():
      message = "Category names cannot start or end with whitespace"
      raise OrganizerError(f"{message}: {category!r}")
    category_names.append(category.casefold())
    if not isinstance(items, list) or not items:
      raise OrganizerError(f"Empty or invalid category: {category!r}")
    for item in items:
      required_fields = ("repository", "description", "url")
      valid_item = isinstance(item, dict) and all(
        isinstance(item.get(field), str) and item[field].strip()
        for field in required_fields
      )
      if not valid_item:
        raise OrganizerError(f"Invalid repository record in {category}.")
      validate_repository_name(item["repository"])
      expected_url = "https://github.com/" + item["repository"]
      if item["url"].rstrip("/").casefold() != expected_url.casefold():
        raise OrganizerError("Repository URL must match " + expected_url)
      names.append(item["repository"])
  if len(category_names) != len(set(category_names)):
    raise OrganizerError("Category names must be unique, ignoring capitalization.")
  folded_names = Counter(name.casefold() for name in names)
  duplicates = [name for name, count in folded_names.items() if count > 1]
  if duplicates:
    raise OrganizerError("Duplicate assignments: " + ", ".join(duplicates))
  if expected is not None:
    missing, extra = set(expected) - set(names), set(names) - set(expected)
    if missing or extra:
      missing_names = ", ".join(sorted(missing)) or "none"
      extra_names = ", ".join(sorted(extra)) or "none"
      message = (
        "Plan does not match the source.\n"
        f"Uncategorized: {missing_names}\n"
        f"No longer starred or renamed: {extra_names}"
      )
      raise OrganizerError(message)
  return names


def load_plan(allow_empty=False):
  plan = load_json(data_path("category-plan.json"))
  if allow_empty and plan == {}:
    return plan
  validate_plan(plan)
  return plan


def load_descriptions(plan):
  descriptions = load_json(data_path("category-descriptions.json"))
  validate_descriptions(plan, descriptions)
  return descriptions


def validate_descriptions(plan, descriptions):
  """Require exactly one nonempty purpose description per category."""
  if not isinstance(descriptions, dict):
    raise OrganizerError("Category descriptions must be a JSON object.")
  missing, extra = set(plan) - set(descriptions), set(descriptions) - set(plan)
  if missing or extra:
    missing_names = ", ".join(sorted(missing)) or "none"
    extra_names = ", ".join(sorted(extra)) or "none"
    raise OrganizerError(
      "Category descriptions must match the plan. "
      f"Missing: {missing_names}; unused: {extra_names}"
    )
  if any(not isinstance(value, str) or not value.strip() for value in descriptions.values()):
    raise OrganizerError("Every category needs a nonempty description.")


def load_visibility(requested=None):
  """Use an explicit choice, then the saved preference, then private."""
  if requested is not None:
    return requested
  path = data_path("list-visibility.json")
  if not path.exists():
    return "private"
  preference = load_json(path)
  if not isinstance(preference, dict):
    raise OrganizerError(f"{path} must contain a JSON object.")
  setting = preference.get("visibility", "private")
  if setting not in ("private", "public"):
    raise OrganizerError(f"Invalid visibility in {path}: choose private or public.")
  return setting


# Local exports and categorization


def local_names():
  path = data_path("starred-repos.csv")
  names = []
  try:
    with path.open(newline="", encoding="utf-8-sig") as handle:
      for row in csv.reader(handle, skipinitialspace=True):
        if not row:
          continue
        validate_repository_name(row[0])
        names.append(row[0])
  except FileNotFoundError as exc:
    message = "Source export is missing. Run init for a new setup or export to refresh."
    raise OrganizerError(message) from exc
  if len(names) != len(set(names)):
    raise OrganizerError("The source CSV contains duplicate repositories.")
  return names


def render(plan):
  names = validate_plan(plan, local_names())
  descriptions = load_descriptions(plan)
  lines = []
  rows = []
  for category, items in plan.items():
    lines.append(category + " — " + descriptions[category])
    for item in sorted(items, key=lambda r: r["repository"].casefold()):
      lines.append(item["repository"] + " — " + item["description"])
      rows.append([category, item["repository"], item["description"], item["url"]])
    lines.append("")
  lines += [
    "Summary",
    f"Total repositories processed: {len(names)}",
    f"Total repositories categorized: {len(names)}",
    f"Number of categories: {len(plan)}",
    "Missing: 0",
    "Duplicates: 0",
    "Empty categories: 0",
  ]
  audit_path = data_path("categorization-audit.json")
  if audit_path.exists():
    audit_data = load_json(audit_path)
    if not isinstance(audit_data, dict):
      raise OrganizerError(f"{audit_path} must contain a JSON object.")
    notes = audit_data.get("judgment_calls", [])
    valid_notes = isinstance(notes, list) and all(
      isinstance(note, str) for note in notes
    )
    if not valid_notes:
      raise OrganizerError(f"judgment_calls in {audit_path} must be a list of strings.")
    if notes:
      lines += ["", "Judgment calls (initial review)"] + ["- " + n for n in notes]
    unread_readmes = audit_data.get("unread_readmes", [])
    valid_unread_readmes = isinstance(unread_readmes, list) and all(
      isinstance(name, str) for name in unread_readmes
    )
    if not valid_unread_readmes:
      message = f"unread_readmes in {audit_path} must be a list of repository names."
      raise OrganizerError(message)
    if unread_readmes:
      lines += ["", "README files not inspected"]
      lines.extend("- " + name for name in unread_readmes)
  lines += ["", f"Completeness check: {len(names)} processed = {len(names)} represented.", ""]
  output_dir = data_path("categorized-stars.txt").parent
  output_dir.mkdir(parents=True, exist_ok=True)
  data_path("categorized-stars.txt").write_text("\n".join(lines), encoding="utf-8")
  with data_path("categorized-stars.csv").open("w", newline="", encoding="utf-8") as handle:
    writer = csv.writer(handle)
    writer.writerow(["category", "category_description", "repository", "description", "url"])
    for row in rows:
      writer.writerow([row[0], descriptions[row[0]], *row[1:]])


def write_export(stars, plan, show_changes=True):
  if len(stars) != len({r["full_name"] for r in stars}):
    raise OrganizerError("GitHub returned duplicate stars; retry the export.")
  assigned = set(validate_plan(plan)) if plan else set()
  save_json(data_path("starred-metadata.json"), stars)
  temp = data_path("starred-repos.csv").with_suffix(".csv.tmp")
  with temp.open("w", newline="", encoding="utf-8") as handle:
    writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
    for repository in stars:
      writer.writerow([
        repository["full_name"],
        repository["html_url"],
        repository.get("description") or "",
        repository.get("language") or "",
        repository["stargazers_count"],
        repository["forks_count"],
      ])
  temp.replace(data_path("starred-repos.csv"))
  current = {r["full_name"] for r in stars}
  pending = [r for r in stars if r["full_name"] not in assigned]
  save_json(data_path("uncategorized-repos.json"), pending)
  print(f"Exported {len(stars)} stars. Uncategorized: {len(pending)}.")
  if show_changes:
    for name in sorted(current - assigned):
      print("  NEW " + name)
    for name in sorted(assigned - current):
      print("  NO LONGER STARRED / RENAMED " + name)


def export_stars():
  plan = load_plan(allow_empty=True)
  write_export(fetch_stars(), plan)


def initialize_for_account():
  """Back up any existing local plan, then create a fresh plan for this CLI account."""
  stars = fetch_stars()
  if not stars:
    message = "No starred repositories found. Star a repository, then run init again."
    raise OrganizerError(message)
  if len(stars) != len({repo["full_name"] for repo in stars}):
    raise OrganizerError("GitHub returned duplicate stars; retry init.")
  stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
  existing_files = [data_path(name) for name in DATA_FILES if data_path(name).exists()]
  if existing_files:
    backup_dir = data_path("backups") / ("initial-plan-" + stamp)
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in existing_files:
      shutil.copy2(path, backup_dir / path.name)
    print(f"Backed up {len(existing_files)} existing local files to {backup_dir}.")
  else:
    print("No existing local plan or exports to back up.")
  save_json(data_path("category-plan.json"), {})
  save_json(data_path("category-descriptions.json"), {})
  save_json(data_path("list-visibility.json"), {"visibility": "private"})
  write_export(stars, {}, show_changes=False)
  # These files describe the previous account or taxonomy; the backup preserves them.
  for name in (
    "categorized-stars.txt", "categorized-stars.csv", "categorization-audit.json",
    "github-lists-after.json", "last-sync.json",
  ):
    data_path(name).unlink(missing_ok=True)
  print("Started a fresh local taxonomy for the authenticated GitHub account.")
  print(f"Review {len(stars)} repositories with: python3 github_stars.py review")
  print("Next: fill data/category-plan.json and data/category-descriptions.json.")
  print("Then run check, render, diff, apply, and verify.")


def review_unassigned():
  """List full metadata for every exported star that needs a category decision."""
  metadata_path = data_path("starred-metadata.json")
  if not metadata_path.exists():
    raise OrganizerError("Run 'python3 github_stars.py export' first.")
  plan = load_plan(allow_empty=True)
  assigned = {r["repository"] for items in plan.values() for r in items}
  metadata = load_json(metadata_path)
  if not isinstance(metadata, list) or any(not isinstance(repo, dict) for repo in metadata):
    raise OrganizerError("Invalid repository metadata. Run export to refresh it.")
  pending = [r for r in metadata if r.get("full_name") not in assigned]
  print(f"Unassigned repositories: {len(pending)} of {len(metadata)}\n")
  for r in pending:
    print(r.get("full_name", "(repository name unavailable)"))
    print("  " + (r.get("html_url") or ""))
    print("  Description: " + (r.get("description") or "(none; inspect its README)"))
    print("  Language: " + (r.get("language") or "(not reported)"))
    print("  Topics: " + (", ".join(r.get("topics") or []) or "(none)"))


def assign_repository(plan, repository, category, description, category_description=None):
  category = category.strip()
  if repository not in local_names():
    message = "Repository is absent from the export; run export first: "
    raise OrganizerError(message + repository)
  record = next(
    (repo for items in plan.values() for repo in items
     if repo["repository"] == repository),
    None,
  )
  if record is None:
    if not description:
      message = (
        "New assignments require --description with a reviewed "
        "one-sentence description."
      )
      raise OrganizerError(message)
    record = {
      "repository": repository,
      "url": "https://github.com/" + repository,
      "description": description,
    }
  elif description:
    record = {**record, "description": description}
  if not category:
    raise OrganizerError("Category name cannot be empty.")
  descriptions = load_descriptions(plan)
  category_is_new = category not in plan
  if category_is_new and not (category_description and category_description.strip()):
    raise OrganizerError("A new category requires --category-description with its purpose.")
  for items in plan.values():
    items[:] = [r for r in items if r["repository"] != repository]
  plan.setdefault(category, []).append(record)
  plan = {name: items for name, items in plan.items() if items}
  if category_is_new:
    descriptions[category] = category_description.strip()
  for old_category in set(descriptions) - set(plan):
    del descriptions[old_category]
  validate_plan(plan)
  save_json(data_path("category-plan.json"), plan)
  save_json(data_path("category-descriptions.json"), descriptions)
  print(f"Assigned {repository} → {category} locally. Run render, then diff.")


def rename_category(plan, old_name, new_name, description=None):
  """Rename a local category or merge it into another category."""
  new_name = new_name.strip()
  if old_name not in plan or not new_name:
    raise OrganizerError("Supply an existing source category and a nonempty target name.")
  if old_name == new_name and description is None:
    print("Category name is unchanged.")
    return

  descriptions = load_descriptions(plan)
  if old_name != new_name:
    plan.setdefault(new_name, []).extend(plan.pop(old_name))
    old_description = descriptions.pop(old_name)
    descriptions.setdefault(new_name, old_description)
  if description is not None:
    descriptions[new_name] = description.strip()
  validate_plan(plan)
  validate_descriptions(plan, descriptions)
  save_json(data_path("category-plan.json"), plan)
  save_json(data_path("category-descriptions.json"), descriptions)
  print("Updated the local plan. Run render, then diff.")


def validate_snapshot(plan, stars, snapshot):
  names = set(validate_plan(plan, [r["full_name"] for r in stars]))
  if len(stars) != len({r["full_name"] for r in stars}):
    raise OrganizerError("GitHub returned duplicate stars; retry the snapshot.")
  for entry in snapshot["lists"]:
    for item in entry["items"]["nodes"]:
      if item.get("nameWithOwner") not in names:
        raise OrganizerError(
          f"Review this unexpected list item before syncing: {item!r} in {entry['name']}"
        )


def validate_memberships(plan, snapshot):
  """Require the complete live list membership to match the local plan."""
  actual = {entry["name"]: entry for entry in snapshot["lists"]}
  if set(actual) != set(plan) or len(actual) != len(snapshot["lists"]):
    raise OrganizerError("Live category names do not exactly match the plan.")
  for category, repositories in plan.items():
    expected = Counter(repo["repository"] for repo in repositories)
    observed = Counter(
      repo["nameWithOwner"] for repo in actual[category]["items"]["nodes"]
    )
    if observed != expected or not observed:
      raise OrganizerError("Incorrect or empty live category: " + category)


def match_category_lists(plan, snapshot):
  """Choose existing lists to keep or reuse, in the same order for diff and apply."""
  existing = {entry["name"]: entry for entry in snapshot["lists"]}
  spare_lists = iter(entry for entry in snapshot["lists"] if entry["name"] not in plan)
  return {
    category: existing[category] if category in existing else next(spare_lists, None)
    for category in plan
  }


def repository_memberships(snapshot):
  """Index current memberships by repository name using stable list IDs."""
  memberships = {}
  for entry in snapshot["lists"]:
    for repository in entry["items"]["nodes"]:
      memberships.setdefault(repository["nameWithOwner"], set()).add(entry["id"])
  return memberships


def is_private(visibility):
  return visibility == "private"


def report_diff(plan, snapshot, visibility="private"):
  descriptions = load_descriptions(plan)
  desired_private = is_private(visibility)
  existing = {entry["name"]: entry for entry in snapshot["lists"]}
  selected_lists = match_category_lists(plan, snapshot)
  list_names = {entry["id"]: entry["name"] for entry in snapshot["lists"]}
  reused_names = set()
  memberships = repository_memberships(snapshot)
  changes = 0
  list_changes = 0
  for category, items in plan.items():
    selected = selected_lists[category]
    if category not in existing:
      if selected is not None:
        reused_names.add(selected["name"])
        print(
          f"RENAME AND SET {visibility.upper()} "
          f"{selected['name']} → {category}"
        )
      else:
        print(f"CREATE {visibility.upper()} LIST " + category)
      list_changes += 1
    elif existing[category]["isPrivate"] != desired_private:
      print(f"MAKE {visibility.upper()} " + category)
      list_changes += 1
    has_changed_description = (
      category in existing
      and existing[category].get("description") != descriptions[category]
    )
    if has_changed_description:
      print(f"UPDATE DESCRIPTION {category}: {descriptions[category]}")
      list_changes += 1
    for item in items:
      previous = memberships.get(item["repository"], set())
      if selected is None or previous != {selected["id"]}:
        changes += 1
        previous_names = ", ".join(sorted(list_names[list_id] for list_id in previous))
        previous_names = previous_names or "(unlisted)"
        print(f"  {item['repository']}: {previous_names} → {category}")
  obsolete = existing.keys() - plan.keys() - reused_names
  for category in sorted(obsolete):
    print(f"DELETE EMPTY LIST AFTER REASSIGNMENT {category}")
    list_changes += 1
  print(
    f"{len(plan)} {visibility} categories; {changes} repository assignments and "
    f"{list_changes} list changes planned."
  )


# GitHub writes and verification


def update_category_descriptions(plan, visibility="private"):
  """Set list descriptions while retaining every list's current membership."""
  descriptions = load_descriptions(plan)
  snapshot, stars = fetch_lists(), fetch_stars()
  validate_snapshot(plan, stars, snapshot)
  names = {entry["name"] for entry in snapshot["lists"]}
  if names != set(plan):
    message = "GitHub category names do not match the local plan; run diff and apply first."
    raise OrganizerError(message)
  validate_memberships(plan, snapshot)
  backup_snapshot(plan, stars, snapshot)
  save_json(data_path("list-visibility.json"), {"visibility": visibility})
  by_name = {entry["name"]: entry for entry in snapshot["lists"]}
  for category, description in descriptions.items():
    entry = by_name[category]
    already_current = (
      entry.get("description") == description
      and entry["isPrivate"] == is_private(visibility)
    )
    if already_current:
      continue
    query = '''mutation($input:UpdateUserListInput!) {
          updateUserList(input:$input) { list { id name description isPrivate } }
        }'''
    list_input = {
      "listId": entry["id"],
      "description": description,
      "isPrivate": is_private(visibility),
    }
    result = graphql(query, {"input": list_input})["updateUserList"]["list"]
    saved_description = result.get("description") == description
    saved_visibility = result.get("isPrivate") == is_private(visibility)
    if not saved_description or not saved_visibility:
      message = f"GitHub did not save the {visibility} list description: {category}"
      raise OrganizerError(message)
    print("Updated " + category)
  updated = fetch_lists()
  verify(plan, updated, fetch_stars(), visibility)
  save_json(data_path("github-lists-after.json"), updated)
  save_json(data_path("list-visibility.json"), {"visibility": visibility})
  message = (
    f"Updated and verified descriptions for all {len(plan)} {visibility} lists; "
    "repository memberships are preserved."
  )
  print(message)


def verify(plan, snapshot, stars, visibility="private"):
  validate_snapshot(plan, stars, snapshot)
  validate_memberships(plan, snapshot)
  descriptions = load_descriptions(plan)
  actual = {entry["name"]: entry for entry in snapshot["lists"]}
  for category in plan:
    entry = actual[category]
    if entry["isPrivate"] != is_private(visibility):
      raise OrganizerError(f"Category is not {visibility}: " + category)
    if entry.get("description") != descriptions[category]:
      raise OrganizerError("Incorrect category description: " + category)
  count = sum(len(items) for items in plan.values())
  print(
    f"VERIFIED: {len(stars)} starred = {count} categorized; "
    f"{len(plan)} {visibility} nonempty categories; no duplicates or omissions."
  )


def set_category_visibility(snapshot, plan, visibility):
  """Set visibility on existing lists that will remain in the plan."""
  desired_private = is_private(visibility)
  for entry in snapshot["lists"]:
    if entry["name"] not in plan or entry["isPrivate"] == desired_private:
      continue

    query = '''mutation($input:UpdateUserListInput!) {
          updateUserList(input:$input) { list { id isPrivate } }
        }'''
    variables = {"input": {"listId": entry["id"], "isPrivate": desired_private}}
    result = graphql(query, variables)["updateUserList"]["list"]
    if result["isPrivate"] != desired_private:
      raise OrganizerError(f"GitHub did not set list visibility to {visibility}; stopped.")


def prepare_category_lists(plan, snapshot, descriptions, visibility):
  """Reuse, rename, or create one GitHub list for every planned category."""
  existing = {entry["name"]: entry for entry in snapshot["lists"]}
  desired_private = is_private(visibility)
  selected_lists = match_category_lists(plan, snapshot)
  list_ids = {}
  descriptions_set = set()

  for category in plan:
    if category in existing:
      list_ids[category] = existing[category]["id"]
      continue

    entry = selected_lists[category]
    if entry is not None:
      query = '''mutation($input:UpdateUserListInput!) {
              updateUserList(input:$input) { list { id name isPrivate } }
            }'''
      list_input = {
        "listId": entry["id"],
        "name": category,
        "description": descriptions[category],
        "isPrivate": desired_private,
      }
      result = graphql(query, {"input": list_input})["updateUserList"]["list"]
      error_message = f"Renamed list is not {visibility}; stopped."
    else:
      query = '''mutation($input:CreateUserListInput!) {
              createUserList(input:$input) { list { id name isPrivate } }
            }'''
      list_input = {
        "name": category,
        "description": descriptions[category],
        "isPrivate": desired_private,
      }
      result = graphql(query, {"input": list_input})["createUserList"]["list"]
      error_message = f"Created list is not {visibility}; stopped."

    if result["isPrivate"] != desired_private:
      raise OrganizerError(error_message)
    list_ids[category] = result["id"]
    descriptions_set.add(category)

  return list_ids, existing, descriptions_set


def update_list_descriptions(list_ids, existing, descriptions_set, descriptions, visibility):
  """Refresh descriptions on matching lists; new and renamed lists are done."""
  desired_private = is_private(visibility)
  query = '''mutation($input:UpdateUserListInput!) {
      updateUserList(input:$input) { list { id name description isPrivate } }
    }'''

  for category, list_id in list_ids.items():
    if category in descriptions_set:
      continue
    if existing[category].get("description") == descriptions[category]:
      continue

    list_input = {
      "listId": list_id,
      "name": category,
      "description": descriptions[category],
      "isPrivate": desired_private,
    }
    result = graphql(query, {"input": list_input})["updateUserList"]["list"]
    if (
      result.get("description") != descriptions[category]
      or result.get("isPrivate") != desired_private
    ):
      raise OrganizerError("GitHub did not save the category description: " + category)


def sync_memberships(plan, stars, snapshot, list_ids):
  """Move changed repositories into exactly their planned list."""
  live_repositories = {repo["full_name"]: repo for repo in stars}
  current_lists = repository_memberships(snapshot)

  changes = []
  for category, repositories in plan.items():
    list_id = list_ids[category]
    for repo in repositories:
      current = current_lists.get(repo["repository"], set())
      if current != {list_id}:
        changes.append((list_id, repo["repository"]))

  for start in range(0, len(changes), 10):
    batch = changes[start:start + 10]
    fields = []
    for index, (list_id, repository) in enumerate(batch):
      node_id = live_repositories[repository]["node_id"]
      fields.append(
        f"r{index}: updateUserListsForItem(input:{{itemId:{json.dumps(node_id)},"
        f"listIds:[{json.dumps(list_id)}]}}) {{ clientMutationId }}"
      )
    graphql("mutation { " + " ".join(fields) + " }")
    completed = min(start + len(batch), len(changes))
    print(f"Assigned {completed}/{len(changes)} changed repositories.", flush=True)


def delete_empty_obsolete_lists(list_ids, snapshot):
  """Remove obsolete lists only after confirming they contain no items."""
  obsolete_ids = {entry["id"] for entry in snapshot["lists"]} - set(list_ids.values())
  for entry in fetch_lists()["lists"]:
    if entry["id"] not in obsolete_ids:
      continue
    if entry["items"]["totalCount"]:
      raise OrganizerError("Refusing to delete a nonempty obsolete list: " + entry["name"])

    query = '''mutation($id:ID!) {
          deleteUserList(input:{listId:$id}) { clientMutationId }
        }'''
    graphql(query, {"id": entry["id"]})


def backup_snapshot(plan, stars, snapshot):
  """Save a recovery snapshot before the first remote write."""
  stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
  backup = data_path("backups") / (stamp + ".json")
  backup_data = {
    "snapshot": snapshot,
    "stars": [repo["full_name"] for repo in stars],
    "plan": plan,
  }
  save_json(backup, backup_data)
  print("Backup: " + str(backup), flush=True)
  return stamp, backup


def apply(plan, stars, snapshot, visibility="private"):
  validate_snapshot(plan, stars, snapshot)
  descriptions = load_descriptions(plan)
  stamp, backup = backup_snapshot(plan, stars, snapshot)
  save_json(data_path("list-visibility.json"), {"visibility": visibility})
  set_category_visibility(snapshot, plan, visibility)
  list_ids, existing, descriptions_set = prepare_category_lists(
    plan, snapshot, descriptions, visibility
  )
  update_list_descriptions(
    list_ids, existing, descriptions_set, descriptions, visibility
  )
  sync_memberships(plan, stars, snapshot, list_ids)
  delete_empty_obsolete_lists(list_ids, snapshot)
  after, stars_after = fetch_lists(), fetch_stars()
  verify(plan, after, stars_after, visibility)
  save_json(data_path("github-lists-after.json"), after)
  sync_summary = {
    "applied_at": stamp,
    "account": after["login"],
    "repository_count": len(stars_after),
    "category_count": len(plan),
    "visibility": visibility,
    "missing": 0,
    "duplicates": 0,
    "backup": str(backup.relative_to(DATA_DIR)),
  }
  save_json(data_path("last-sync.json"), sync_summary)


def build_parser():
  """Build the command-line interface."""
  parser = argparse.ArgumentParser(description=__doc__)
  sub = parser.add_subparsers(dest="command", required=True)
  visibility_commands = {"describe", "diff", "apply", "verify"}
  for name, help_text in {
    "init": "Start a fresh local taxonomy for the authenticated account.",
    "export": "Refresh stars and metadata; report additions without changing assignments.",
    "review": "Show metadata for each repository that still needs categorization.",
    "check": "Check local completeness, uniqueness, and nonempty categories.",
    "render": "Rebuild the readable catalog and categorized CSV.",
    "describe": "Set list descriptions and visibility without changing memberships.",
    "diff": "Preview live changes and desired visibility without writing to GitHub.",
    "apply": "Apply the plan to GitHub lists and verify the selected visibility.",
    "verify": "Check live membership, completeness, descriptions, and visibility.",
  }.items():
    command_parser = sub.add_parser(name, help=help_text, description=help_text)
    if name in visibility_commands:
      command_parser.add_argument(
        "--visibility",
        choices=("private", "public"),
        default=None,
        help="List visibility (default: saved choice, or private).",
      )
  assign = sub.add_parser("assign", help="Assign or move one repository locally.")
  assign.add_argument("repository", help="Exact owner/repository name from your export.")
  assign.add_argument("category", help="Destination category; quote names containing spaces.")
  assign.add_argument("--description", help="Repository purpose; required for a new assignment.")
  assign.add_argument("--category-description", help="Purpose of a new category.")
  rename = sub.add_parser(
    "rename", help="Rename a local category, merging into an existing target."
  )
  rename.add_argument("old")
  rename.add_argument("new")
  rename.add_argument("--description", help="Purpose description for the renamed category.")
  return parser


def handle_local_command(args, plan):
  """Run commands that only update local files or display local data."""
  if args.command == "assign":
    assign_repository(
      plan, args.repository, args.category, args.description,
      args.category_description,
    )
  elif args.command == "rename":
    rename_category(plan, args.old, args.new, args.description)
  elif args.command in ("check", "render"):
    names = validate_plan(plan, local_names())
    load_descriptions(plan)
    if args.command == "render":
      render(plan)
    print(
      f"PASS: {len(names)} repositories, {len(plan)} described nonempty "
      "categories, no duplicates or omissions."
    )


def handle_github_command(args, plan):
  """Run a command that reads from or writes to GitHub."""
  visibility = load_visibility(args.visibility)
  if args.command == "describe":
    update_category_descriptions(plan, visibility)
    return

  stars, snapshot = fetch_stars(), fetch_lists()
  validate_snapshot(plan, stars, snapshot)
  if args.command == "diff":
    report_diff(plan, snapshot, visibility)
  elif args.command == "verify":
    verify(plan, snapshot, stars, visibility)
  elif args.command == "apply":
    apply(plan, stars, snapshot, visibility)


def main(argv=None):
  args = build_parser().parse_args(argv)
  if args.command == "export":
    export_stars()
    return
  if args.command == "init":
    initialize_for_account()
    return
  if args.command == "review":
    review_unassigned()
    return

  plan = load_plan(allow_empty=args.command == "assign")
  local_commands = {"assign", "rename", "check", "render"}
  if args.command in local_commands:
    handle_local_command(args, plan)
  else:
    handle_github_command(args, plan)


if __name__ == "__main__":
  try:
    main()
  except (OrganizerError, OSError, ValueError, KeyError) as exc:
    print("ERROR: " + str(exc), file=sys.stderr)
    sys.exit(1)
