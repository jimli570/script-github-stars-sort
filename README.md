# GitHub stars organizer

Export your starred repositories, categorize them with an AI agent or by hand, and apply the result to GitHub lists. Every repository gets one category and a short description. Lists default to **private**, with an explicit option for public lists.

All account data stays in the ignored `data/` folder beside the script. The tool never stars, unstars, or deletes repositories. Categorization requires your review or an AI agent; the script does not infer repository purposes automatically.

## Requirements

- Python 3.9 or newer. No Python packages are required.
- [GitHub CLI](https://cli.github.com/) (`gh`), authenticated to your account on GitHub.com.
- Network access to GitHub for `init`, `export`, `diff`, `apply`, `describe`, and `verify`.

Clone or download this project and open a terminal in its folder. On Windows, use `python` instead of `python3` if that is how Python is installed. The optional shell wrapper requires Bash; the Python script works directly on Windows.

## First run

### 1. Connect your account and export your stars

```bash
gh auth login -h github.com
gh auth refresh -h github.com -s user
gh auth status -h github.com
python3 github_stars.py init
```

Check that `gh auth status` shows the account you intend to organize. `init` exports your stars and creates empty category files in `data/`. It backs up an existing setup under `data/backups/` before resetting it, including old catalogs and audit notes. It does not change your GitHub lists.

Use `init` for a fresh setup. Use `export` for later updates so your assignments are preserved. Keep a separate project copy for each GitHub account.

### 2. Categorize every repository

Give an AI coding agent access to this project folder and paste the following prompt. It writes local files for you to review. If you prefer to categorize manually, use the [file format below](#edit-categories-manually).

```text
Organize my GitHub stars using the complete export in data/starred-metadata.json.

1. Count every repository. Determine what each one actually does from its name, description, topics, primary language, and README. Inspect its README when the metadata is insufficient. Do not guess from a name or language alone.
2. Create a practical taxonomy based on this collection. Use short, specific, purpose-based names. Aim for roughly 10–25 categories when the collection supports it; merge overlaps and avoid tiny categories unless truly distinct. If a plan already exists, preserve useful categories and reviewed descriptions while incorporating new repositories.
3. Write data/category-plan.json as an object mapping category names to lists of repository entries. Each entry must contain repository (the exact owner/name from the export), url (https://github.com/owner/name), and description (one accurate sentence).
4. Write data/category-descriptions.json as an object with the same category names and one short sentence explaining what belongs in each category.
5. Include every exported repository exactly once. Leave no empty categories. Investigate difficult cases instead of omitting them. Review renamed or no-longer-starred entries against the latest export before updating the local plan. Do not star, unstar, delete, or change any GitHub lists.
6. Write data/categorization-audit.json with judgment_calls (a list of short notes about ambiguous decisions) and unread_readmes (a list of repository names whose README could not be inspected). Use empty lists when there are none.
7. Use valid JSON without duplicate keys, comments, or trailing commas. Run python3 github_stars.py check and fix all errors, then run python3 github_stars.py render.
8. Report repository and category counts, judgment calls, and any README that could not be inspected. Confirm that the exported repository count equals the count represented in the categories. Do not run apply or describe; I will review the results and choose visibility myself.
```

If the agent has no internet access, investigate unclear repositories yourself before approving their assignments. `python3 github_stars.py review` shows metadata for all unassigned repositories.

### 3. Review and apply

```bash
python3 github_stars.py check
python3 github_stars.py render
python3 github_stars.py diff
```

Read `data/categorized-stars.txt` and the diff. **The tool manages all your GitHub star lists.** The diff shows which lists will be created, renamed, or removed, plus membership, description, and visibility changes. Obsolete lists are removed only after their repositories have been reassigned and the lists are empty.

Apply the reviewed plan:

```bash
python3 github_stars.py apply
```

`apply` writes to GitHub immediately, saves a recovery snapshot first, and verifies the final memberships, descriptions, and visibility. Open your GitHub profile's **Stars** page to browse the resulting lists. You can check the live result again with `python3 github_stars.py verify`.

### Choose private or public lists

A fresh setup defaults to private. To publish your category lists:

```bash
python3 github_stars.py diff --visibility public
python3 github_stars.py apply --visibility public
```

To switch back, use `--visibility private` with the same commands. `apply` and `describe` save the chosen visibility before making remote changes, so interrupted runs retain your choice. Later `diff`, `apply`, `describe`, and `verify` commands reuse it. A visibility flag on `diff` or `verify` is a one-time override and is not saved.

## Re-sort after adding or changing stars

```bash
python3 github_stars.py export
python3 github_stars.py review
```

Review `data/uncategorized-repos.json` and update the plan using the agent prompt or the editing commands below. `export` preserves existing assignments and reports renamed or no-longer-starred entries. Then run:

```bash
python3 github_stars.py check
python3 github_stars.py render
python3 github_stars.py diff
python3 github_stars.py apply
```

The tool refuses to apply an incomplete or duplicated plan. Repeated applies skip repository memberships that already match.

## Edit categories manually

`data/category-plan.json`:

```json
{
  "Developer Tools": [
    {
      "repository": "owner/repository",
      "url": "https://github.com/owner/repository",
      "description": "One sentence explaining what this project does."
    }
  ]
}
```

`data/category-descriptions.json`:

```json
{
  "Developer Tools": "Tools for building, testing, and maintaining software."
}
```

Replace the example repository with one in your export. Category names must match between the files, and every exported repository must appear once in the plan.

Move an already categorized repository while retaining its description:

```bash
python3 github_stars.py assign owner/repository "Developer Tools"
```

For a new repository or category, supply the descriptions:

```bash
python3 github_stars.py assign owner/repository "Category Name" --description "What the repository does." --category-description "What belongs in this category."
```

`--description` is required for a new assignment. `--category-description` is required only when creating a category. Moving its final repository removes the empty category locally.

Rename or merge categories:

```bash
python3 github_stars.py rename "Media & Audio" "Audio & Video"
```

If the target already exists, its purpose description is retained. Otherwise the old description carries over. Use `--description "New purpose."` to replace it. These editing commands only change local files; run `check`, `render`, `diff`, and `apply` afterward.

For description-only edits, change `data/category-descriptions.json`, run `render` and `diff`, then run `python3 github_stars.py describe`. This updates descriptions and the selected visibility while preserving memberships; it requires the live memberships to match the plan first.

## Files and backups

| Path                              | Purpose                                                                             |
| --------------------------------- | ----------------------------------------------------------------------------------- |
| `github_stars.py`                 | All application code and CLI commands.                                              |
| `data/category-plan.json`         | Repository assignments and descriptions.                                            |
| `data/category-descriptions.json` | Purpose of each category.                                                           |
| `data/list-visibility.json`       | Saved visibility preference.                                                        |
| `data/starred-metadata.json`      | Complete repository metadata from the latest export.                                |
| `data/starred-repos.csv`          | Source export: repository, URL, description, language, stars, and forks; no header. |
| `data/uncategorized-repos.json`   | Exported repositories missing from the plan at export time.                         |
| `data/categorized-stars.txt`      | Readable catalog and completeness summary.                                          |
| `data/categorized-stars.csv`      | Spreadsheet-friendly catalog with a header row.                                     |
| `data/categorization-audit.json`  | Optional agent notes on judgment calls and unread READMEs.                          |
| `data/backups/`                   | Previous local setup files and snapshots taken before `apply` or `describe`.        |
| `data/github-lists-after.json`    | Last verified live list snapshot.                                                   |
| `data/last-sync.json`             | Last successful apply summary; its backup path is relative to `data/`.              |

The entire `data/` folder is ignored by Git. No command commits or pushes files. Running the script from another working directory still uses the `data/` folder beside the script.

If upgrading a version that stored generated files at the project root, move the account files listed above into `data/` and the old `backups/` folder into `data/backups/`. Preserve both copies if a destination already exists. Do not use `init` to migrate a plan you want to keep: it starts a fresh one.

## Troubleshooting

- **Insufficient scopes:** run `gh auth refresh -h github.com -s user` and finish browser authorization. For an environment-provided token, update that token's permissions instead.
- **Missing data files:** run `init` for a new setup. For an existing setup, restore the missing plan files from a local backup; use `export` to refresh missing export files.
- **Plan does not match the source:** run `export`, categorize new stars, and review removed or renamed entries. Correct both category files when removing an empty category.
- **No starred repositories:** star at least one repository, then run `init` again. An empty or failed initial fetch leaves an existing plan intact.
- **Interrupted update:** fix the reported error, run `diff`, and retry `apply` (or `describe` for a description update). GitHub changes are not transactional, so some changes may already have succeeded. Recovery snapshots remain in `data/backups/`; they are reference files for manual recovery, not an automatic rollback.

The legacy `bash get_all_stared_repos.sh` command remains an `export` shortcut after initial setup. Run `python3 github_stars.py --help` to see all commands.

## Development checks

Use two-space indentation in code files; `.editorconfig` sets the project defaults.

```bash
python3 -m unittest discover -s tests -v
python3 github_stars.py --help
bash -n get_all_stared_repos.sh
```

Tests use temporary data directories, mock GitHub responses, and reject unexpected network calls. They cover the local setup/edit/render flow, pagination, completeness, visibility, repeat applies, interruption recovery, and list cleanup. The GitHub Actions workflow runs tests and CLI checks on Python 3.9 and 3.13 on Linux and Windows when changes are pushed or proposed in a pull request.
