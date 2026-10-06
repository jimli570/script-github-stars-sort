"""Offline regression tests; every test uses a temporary data directory."""

import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import github_stars as app


def repo(name):
  return {
    "repository": name,
    "description": "An example repository.",
    "url": "https://github.com/" + name,
  }


def star(name, node_id="R1"):
  return {
    "full_name": name,
    "node_id": node_id,
    "html_url": "https://github.com/" + name,
    "description": "An example repository.",
    "language": "Python",
    "topics": ["example"],
    "stargazers_count": 1,
    "forks_count": 0,
  }


def listing(name, repositories, private=True, list_id=None):
  return {
    "id": list_id or name,
    "name": name,
    "description": "Purpose " + name,
    "isPrivate": private,
    "items": {
      "totalCount": len(repositories),
      "nodes": [{"nameWithOwner": name} for name in repositories],
    },
  }


def page_info(has_next=False, cursor=None):
  return {"hasNextPage": has_next, "endCursor": cursor}


class IsolatedTestCase(unittest.TestCase):
  def setUp(self):
    directory = tempfile.TemporaryDirectory(prefix="github-stars-test-")
    self.addCleanup(directory.cleanup)
    self.workspace = Path(directory.name)
    self.enter_patch(app, "DATA_DIR", self.workspace / "data")
    self.enter_patch(app, "gh", side_effect=AssertionError("Unexpected network call"))
    self.output = io.StringIO()
    redirect = contextlib.redirect_stdout(self.output)
    redirect.__enter__()
    self.addCleanup(redirect.__exit__, None, None, None)

  def enter_patch(self, target, name, *args, **kwargs):
    patcher = patch.object(target, name, *args, **kwargs)
    mock = patcher.start()
    self.addCleanup(patcher.stop)
    return mock

  def save_plan(self, plan):
    app.save_json(app.data_path("category-plan.json"), plan)
    descriptions = {name: "Purpose " + name for name in plan}
    app.save_json(app.data_path("category-descriptions.json"), descriptions)


class ValidationTests(IsolatedTestCase):
  def test_rejects_missing_duplicate_and_empty_assignments(self):
    cases = [
      ({"A": [repo("o/a")]}, ["o/a", "o/b"]),
      ({"A": [repo("o/a")], "B": [repo("O/A")]}, None),
      ({"A": []}, None),
      ({" A": [repo("o/a")]}, None),
      ({"A": [repo("o/a")], "a": [repo("o/b")]}, None),
    ]
    for plan, expected in cases:
      with self.subTest(plan=plan), self.assertRaises(app.OrganizerError):
        app.validate_plan(plan, expected)

  def test_rejects_mismatched_repository_urls(self):
    record = {**repo("o/a"), "url": "https://github.com/o/b"}
    with self.assertRaisesRegex(app.OrganizerError, "URL must match"):
      app.validate_plan({"A": [record]})

  def test_duplicate_json_category_keys_do_not_silently_drop_repositories(self):
    path = app.data_path("category-plan.json")
    path.parent.mkdir()
    path.write_text('{"A": [], "A": []}', encoding="utf-8")
    with self.assertRaisesRegex(app.OrganizerError, "Duplicate JSON key"):
      app.load_plan(allow_empty=True)

  def test_only_empty_objects_are_allowed_as_a_new_plan(self):
    for invalid in ([], None, "", 0):
      with self.subTest(invalid=invalid):
        app.save_json(app.data_path("category-plan.json"), invalid)
        with self.assertRaises(app.OrganizerError):
          app.load_plan(allow_empty=True)
    app.save_json(app.data_path("category-plan.json"), {})
    self.assertEqual(app.load_plan(allow_empty=True), {})

  def test_missing_plan_explains_initial_setup(self):
    with self.assertRaisesRegex(app.OrganizerError, "github_stars.py init"):
      app.load_plan()

  def test_decodes_all_pages_on_older_gh(self):
    self.assertEqual(app.decode_stream('[{"id":1}]\n [{"id":2}]'), [{"id": 1}, {"id": 2}])

  def test_graphql_partial_errors_are_not_treated_as_success(self):
    response = {"data": {"first": {}}, "errors": [{"message": "second mutation failed"}]}
    with patch.object(app, "gh", return_value=json.dumps(response)):
      with self.assertRaisesRegex(app.OrganizerError, "second mutation failed"):
        app.graphql("mutation { example }")

  def test_verify_requires_private_and_exact_membership(self):
    plan = {"A": [repo("o/a")], "B": [repo("o/b")]}
    self.save_plan(plan)
    stars = [star("o/a"), star("o/b", "R2")]
    cases = [
      [listing("A", ["o/a"], False), listing("B", ["o/b"])],
      [listing("A", ["o/a", "o/b"]), listing("B", ["o/b"])],
      [listing("A", ["o/a"]), listing("B", [])],
    ]
    for lists in cases:
      with self.subTest(lists=lists), self.assertRaises(app.OrganizerError):
        app.verify(plan, {"lists": lists}, stars)

  def test_paginates_both_lists_and_members(self):
    first = listing("A", ["o/a"])
    first["items"].update(totalCount=2, pageInfo=page_info(True, "items2"))
    second = listing("B", ["o/c"])
    second["items"]["pageInfo"] = page_info()
    responses = [
      {"viewer": {"login": "me", "lists": {
        "nodes": [first], "pageInfo": page_info(True, "lists2"),
      }}},
      {"viewer": {"login": "me", "lists": {
        "nodes": [second], "pageInfo": page_info(),
      }}},
      {"node": {"items": {
        "totalCount": 2,
        "nodes": [{"nameWithOwner": "o/b"}],
        "pageInfo": page_info(),
      }}},
    ]
    with patch.object(app, "graphql", side_effect=responses) as api:
      result = app.fetch_lists()
    self.assertEqual(len(result["lists"]), 2)
    self.assertEqual(len(result["lists"][0]["items"]["nodes"]), 2)
    self.assertEqual(api.call_args_list[1].args[1]["cursor"], "lists2")
    self.assertEqual(api.call_args_list[2].args[1]["cursor"], "items2")


class LocalWorkflowTests(IsolatedTestCase):
  def test_cli_missing_setup_reports_error_from_another_working_directory(self):
    project = self.workspace / "project"
    project.mkdir()
    script = project / "github_stars.py"
    script.write_text(Path(app.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run(
      [sys.executable, str(script), "check"],
      cwd=self.workspace,
      capture_output=True,
      text=True,
      encoding="utf-8",
    )
    self.assertEqual(result.returncode, 1)
    self.assertIn("github_stars.py init", result.stderr)
    # Match the CLI's resolution of Windows short paths and directory symlinks.
    expected_path = script.resolve().parent / "data" / "category-plan.json"
    self.assertIn(str(expected_path), result.stderr)
    self.assertNotIn("Traceback", result.stderr)

  def test_init_assign_check_render_and_export_use_only_data_directory(self):
    self.enter_patch(app, "fetch_stars", return_value=[star("o/a")])
    app.main(["init"])
    self.assertEqual(app.load_visibility(), "private")
    app.main([
      "assign", "o/a", "Developer Tools", "--description", "Sökverktyg för utvecklare.",
      "--category-description", "Verktyg för utveckling.",
    ])
    app.main(["check"])
    app.main(["render"])
    original_plan = app.load_plan()
    app.main(["export"])
    self.assertEqual(app.load_plan(), original_plan)
    self.assertEqual(app.load_json(app.data_path("uncategorized-repos.json")), [])
    catalog = app.data_path("categorized-stars.txt").read_text(encoding="utf-8")
    self.assertIn("Sökverktyg för utvecklare.", catalog)
    self.assertIn("1 processed = 1 represented", catalog)
    self.assertEqual([path.name for path in self.workspace.iterdir()], ["data"])

  def test_init_preserves_backups_and_removes_stale_results(self):
    self.save_plan({"Old": [repo("o/old")]})
    stale_files = (
      "categorized-stars.txt", "categorized-stars.csv", "categorization-audit.json",
      "github-lists-after.json", "last-sync.json",
    )
    for name in stale_files:
      app.data_path(name).write_text("old account data", encoding="utf-8")
    self.enter_patch(app, "fetch_stars", return_value=[star("o/new")])
    app.main(["init"])
    backups = list(app.data_path("backups").iterdir())
    self.assertEqual(len(backups), 1)
    for name in stale_files:
      self.assertFalse(app.data_path(name).exists())
      self.assertEqual((backups[0] / name).read_text(), "old account data")
    self.assertIn("Old", app.load_json(backups[0] / "category-plan.json"))
    self.assertEqual(app.load_plan(allow_empty=True), {})

  def test_failed_or_empty_init_does_not_reset_existing_plan(self):
    plan = {"A": [repo("o/a")]}
    self.save_plan(plan)
    for response in ([], app.OrganizerError("network unavailable")):
      kwargs = {"side_effect": response} if isinstance(response, Exception) else {
        "return_value": response,
      }
      with self.subTest(response=response), patch.object(app, "fetch_stars", **kwargs):
        with self.assertRaises(app.OrganizerError):
          app.initialize_for_account()
      self.assertEqual(app.load_plan(), plan)

  def test_move_removes_empty_category_and_keeps_description(self):
    plan = {"A": [repo("o/a")], "B": [repo("o/b")]}
    self.save_plan(plan)
    app.write_export([star("o/a"), star("o/b", "R2")], plan)
    app.main(["assign", "o/a", "B"])
    result = app.load_plan()
    self.assertEqual(set(result), {"B"})
    self.assertEqual(len(result["B"]), 2)
    self.assertEqual(result["B"][1]["description"], "An example repository.")
    self.assertEqual(app.load_descriptions(result), {"B": "Purpose B"})

  def test_rename_merge_keeps_target_description(self):
    self.save_plan({"A": [repo("o/a")], "B": [repo("o/b")]})
    app.main(["rename", "A", "B"])
    self.assertEqual(set(app.load_plan()), {"B"})
    self.assertEqual(app.load_descriptions(app.load_plan()), {"B": "Purpose B"})

  def test_rename_with_same_name_can_update_description(self):
    self.save_plan({"A": [repo("o/a")]})
    app.main(["rename", "A", "A", "--description", "Updated purpose."])
    self.assertEqual(app.load_descriptions(app.load_plan()), {"A": "Updated purpose."})

  def test_invalid_rename_description_leaves_saved_files_unchanged(self):
    plan = {"A": [repo("o/a")]}
    self.save_plan(plan)
    with self.assertRaises(app.OrganizerError):
      app.main(["rename", "A", "B", "--description", "   "])
    self.assertEqual(app.load_plan(), plan)
    self.assertEqual(app.load_descriptions(plan), {"A": "Purpose A"})

  def test_visibility_defaults_private_and_preserves_explicit_public_choice(self):
    self.assertEqual(app.load_visibility(), "private")
    app.save_json(app.data_path("list-visibility.json"), {"visibility": "public"})
    self.assertEqual(app.load_visibility(), "public")
    self.assertEqual(app.load_visibility("private"), "private")


class ApplyTests(IsolatedTestCase):
  def test_membership_batches_include_every_repository_once(self):
    names = [f"o/repo{number}" for number in range(21)]
    plan = {"A": [repo(name) for name in names]}
    stars = [star(name, f"R{number}") for number, name in enumerate(names)]
    api = self.enter_patch(app, "graphql", return_value={})
    app.sync_memberships(plan, stars, {"lists": []}, {"A": "L1"})
    self.assertEqual(api.call_count, 3)
    queries = " ".join(call.args[0] for call in api.call_args_list)
    for number in range(21):
      self.assertEqual(queries.count(f'itemId:"R{number}",'), 1)

  def test_unknown_list_item_blocks_before_mutations(self):
    with patch.object(app, "graphql") as api, self.assertRaises(app.OrganizerError):
      app.apply(
        {"A": [repo("o/a")]}, [star("o/a")],
        {"lists": [listing("Old", ["o/unstarred"])]},
      )
    api.assert_not_called()
    self.assertFalse(app.data_path("backups").exists())

  def test_idempotent_apply_performs_no_mutations(self):
    plan = {"A": [repo("o/a")]}
    self.save_plan(plan)
    stars = [star("o/a")]
    snapshot = {"login": "me", "lists": [listing("A", ["o/a"])]}
    self.enter_patch(app, "fetch_lists", return_value=snapshot)
    self.enter_patch(app, "fetch_stars", return_value=stars)
    api = self.enter_patch(app, "graphql")
    app.apply(plan, stars, snapshot)
    api.assert_not_called()
    self.assertTrue(app.data_path("last-sync.json").exists())
    sync = app.load_json(app.data_path("last-sync.json"))
    self.assertTrue(app.data_path(sync["backup"]).exists())

  def test_private_creation_reuses_list_and_assigns_only_changed_repositories(self):
    plan = {"A": [repo("o/a")], "B": [repo("o/b")]}
    self.save_plan(plan)
    stars = [star("o/a"), star("o/b", "R2")]
    snapshot = {"login": "me", "lists": [listing("Old", ["o/a", "o/b"], False, "L1")]}
    after = {"login": "me", "lists": [
      listing("A", ["o/a"], True, "L1"), listing("B", ["o/b"], True, "L2"),
    ]}

    def respond(query, variables=None):
      if "updateUserList(" in query:
        values = variables["input"]
        self.assertEqual(values, {
          "listId": "L1", "name": "A", "description": "Purpose A", "isPrivate": True,
        })
        return {"updateUserList": {"list": {"id": "L1", **values}}}
      if "createUserList(" in query:
        self.assertTrue(variables["input"]["isPrivate"])
        return {"createUserList": {"list": {"id": "L2", **variables["input"]}}}
      self.assertIn('itemId:"R2",listIds:["L2"]', query)
      self.assertNotIn('itemId:"R1"', query)
      return {"r0": {"clientMutationId": None}}

    self.enter_patch(app, "graphql", side_effect=respond)
    self.enter_patch(app, "fetch_lists", return_value=after)
    self.enter_patch(app, "fetch_stars", return_value=stars)
    app.apply(plan, stars, snapshot)
    self.assertEqual(app.load_visibility(), "private")

  def test_diff_counts_match_reused_list_membership_changes(self):
    plan = {"A": [repo("o/a")], "B": [repo("o/b")]}
    self.save_plan(plan)
    snapshot = {"lists": [listing("Old", ["o/a", "o/b"], list_id="L1")]}
    api = self.enter_patch(app, "graphql")
    app.report_diff(plan, snapshot)
    self.assertIn("RENAME AND SET PRIVATE Old → A", self.output.getvalue())
    self.assertIn("1 repository assignments", self.output.getvalue())
    api.assert_not_called()

  def test_refuses_to_delete_nonempty_obsolete_list(self):
    plan = {"A": [repo("o/a")]}
    self.save_plan(plan)
    snapshot = {"login": "me", "lists": [listing("A", ["o/a"]), listing("Old", ["o/a"])]}
    api = self.enter_patch(app, "graphql", return_value={})
    self.enter_patch(app, "fetch_lists", return_value=snapshot)
    with self.assertRaisesRegex(app.OrganizerError, "Refusing to delete"):
      app.apply(plan, [star("o/a")], snapshot)
    self.assertFalse(any("deleteUserList" in call.args[0] for call in api.call_args_list))

  def test_cleanup_leaves_concurrently_created_lists_alone(self):
    snapshot = {"lists": [listing("A", ["o/a"], list_id="L1"), listing("Old", [])]}
    after = copy.deepcopy(snapshot)
    after["lists"].append(listing("Created elsewhere", [], list_id="NEW"))
    self.enter_patch(app, "fetch_lists", return_value=after)
    api = self.enter_patch(app, "graphql")
    app.delete_empty_obsolete_lists({"A": "L1"}, snapshot)
    self.assertEqual(api.call_count, 1)
    self.assertEqual(api.call_args.args[1], {"id": "Old"})

  def test_describe_rejects_stale_memberships_before_writing(self):
    plan = {"A": [repo("o/a")], "B": [repo("o/b")]}
    self.save_plan(plan)
    self.enter_patch(app, "fetch_lists", return_value={"lists": [
      listing("A", ["o/b"]), listing("B", ["o/a"]),
    ]})
    self.enter_patch(app, "fetch_stars", return_value=[star("o/a"), star("o/b")])
    api = self.enter_patch(app, "graphql")
    with self.assertRaisesRegex(app.OrganizerError, "Incorrect or empty"):
      app.update_category_descriptions(plan)
    api.assert_not_called()

  def test_interrupted_describe_preserves_backup_and_requested_visibility(self):
    plan = {"A": [repo("o/a")]}
    self.save_plan(plan)
    snapshot = {"login": "me", "lists": [listing("A", ["o/a"])]}
    self.enter_patch(app, "fetch_lists", return_value=snapshot)
    self.enter_patch(app, "fetch_stars", return_value=[star("o/a")])
    self.enter_patch(app, "graphql", side_effect=app.OrganizerError("connection lost"))
    with self.assertRaisesRegex(app.OrganizerError, "connection lost"):
      app.update_category_descriptions(plan, "public")
    self.assertEqual(app.load_visibility(), "public")
    self.assertEqual(len(list(app.data_path("backups").iterdir())), 1)

  def test_explicit_public_apply_is_saved_for_later_commands(self):
    plan = {"A": [repo("o/a")]}
    self.save_plan(plan)
    before = {"login": "me", "lists": [listing("A", ["o/a"])]}
    after = {"login": "me", "lists": [listing("A", ["o/a"], private=False)]}
    self.enter_patch(app, "fetch_lists", return_value=after)
    self.enter_patch(app, "fetch_stars", return_value=[star("o/a")])
    api = self.enter_patch(app, "graphql", return_value={
      "updateUserList": {"list": {"id": "A", "isPrivate": False}},
    })
    app.apply(plan, [star("o/a")], before, "public")
    self.assertFalse(api.call_args.args[1]["input"]["isPrivate"])
    self.assertEqual(app.load_visibility(), "public")


if __name__ == "__main__":
  unittest.main()
