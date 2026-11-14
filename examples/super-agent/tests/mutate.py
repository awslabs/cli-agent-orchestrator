#!/usr/bin/env python3
"""Mutation check: flip one gate condition at a time and confirm a test fails.

Each mutant is a scratch copy of the kit (bin/, mandates.json, resources.json) with one edit;
the real files are never touched. The kit's tests run against it via SUPER_AGENT_KIT.
pytest exit 1 = KILLED, 0 = SURVIVED, anything else = SETUP_ERROR (never counted as a kill).
An unmutated control copy must pass first. Each replacement must match exactly once.

Usage: python3 tests/mutate.py SCRATCH_DIR [LABEL_PREFIX ...]
  (runs serially; writes nothing outside SCRATCH_DIR; prefixes such as `ag_` or `rg_heavy` pick
  a subset, so a long run can be split into chunks, each with its own fresh SCRATCH_DIR;
  =LABEL picks exactly one mutant)
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
TESTS = KIT / "tests"

CM, AG, OB, RG = "check_mandate.py", "automerge_gate.py", "outbox.py", "resource_gate.py"
GL, PH, RO, HD = "go_ledger.py", "plan_hash_check.py", "remap_owners.py", "holds.py"
AE, SS = "api_envelope.py", "state_schema.py"
MJ, RM, PUB, SPB = "../mandates.json", "../README.md", "../templates/publisher.md", "../shepherd-playbook.md"
CPB, PSH, CCO = "../conductor-playbook.md", "../agents/pr_shepherd.md", "../agents/cao_conductor.md"
ARM_T = "../templates/automerge-armer.md"
M = [
    # check_mandate.py
    ("cm_paused", CM, [('if doc.get("paused") is not False:', "if False:")]),
    ("cm_forbidden", CM, [('if action in forbidden:', "if False:")]),
    ("cm_unknown_action", CM, [("    if not candidates:\n", "    if False:\n")]),
    ("cm_holder", CM, [('if m.get("holder") != holder:', "if False:")]),
    ("cm_revoked", CM, [('if m.get("revoked") is not False:', "if False:")]),
    ("cm_granted_by_user", CM, [('if m.get("granted_by") != "user":', "if False:")]),
    ("cm_repo", CM, [('if scope.get("repo") != repo:', "if False:")]),
    ("cm_author", CM, [('if want_author != "*" and want_author != pr_author:', "if False:")]),
    ("cm_base", CM, [('if scope.get("base") != base:', "if False:")]),
    ("cm_observe", CM, [('return "OBSERVE", m["id"]\n            if mode == "act":', 'return "ALLOW", m["id"]\n            if mode == "act":')]),
    ("cm_bad_mode", CM, [('return "DENY", f"bad_mode:{mode}"', 'return "ALLOW", m["id"]')]),
    ("cm_conductor_not_observed", CM, [('if holder == "conductor":', "if False:")]),
    # check_mandate.py: pre-write re-check
    ("cm_expect_head", CM, [("        if exp != rem:\n", "        if False:\n")]),
    ("cm_head_needs_both", CM, [("if (exp is None) != (rem is None):", "if False:")]),
    ("cm_head_sha_format", CM, [("if not SHA_RE.fullmatch(exp) or not SHA_RE.fullmatch(rem):", "if False:")]),
    ("cm_policy_version", CM, [("if want_v is not None and (", "if False and (")]),
    ("cm_go_enforced", CM, [("        if go_reason:\n", "        if False:\n")]),
    ("cm_go_missing", CM, [("    if not rows:\n", "    if False:\n")]),
    ("cm_go_fields", CM, [("        if got != value:\n", "        if False:\n")]),
    ("cm_go_revoked", CM, [('    if go["revoked"] is not False:', "    if False:")]),
    ("cm_go_latest_wins", CM, [("go = rows[-1]", "go = rows[0]")]),
    ("cm_go_needs_pr_head", CM, [('if (extra.get("pr") is None and not extra.get("branch")) or extra.get("expect_head") is None:', "if False:")]),
    ("cm_observe_needs_go", CM, [('                if go is not None:\n                    return "ALLOW"',
                                  '                if True:\n                    return "ALLOW"')]),
    # go_ledger.py
    ("gl_input_validated", GL, [("if not SHA_RE.fullmatch(a.head) or not a.granted_by.strip():", "if False:")]),
    ("gl_pr_digits", GL, [("or (a.pr is not None and not a.pr.isdigit())", "or False")]),
    ("gl_body_hashed", GL, [("body = hashlib.sha256(fh.read()).hexdigest()", "body = None")]),
    ("gl_revoke_recorded", GL, [('append({"id": a.id, "revoked": True,', 'append({"id": a.id, "revoked": False,')]),
    # automerge_gate.py: scope
    ("ag_author_ours", AG, [('if author not in ids["ours"] or (scope_author != "*" and author != scope_author):',
                             'if (scope_author != "*" and author != scope_author):')]),
    ("ag_author_scope", AG, [('if author not in ids["ours"] or (scope_author != "*" and author != scope_author):',
                              'if author not in ids["ours"]:')]),
    ("ag_base_main", AG, [('if pr["baseRefName"] != "main":', "if False:")]),
    ("ag_draft", AG, [('if pr["isDraft"] is not False:', "if False:")]),
    ("ag_never_prs", AG, [('if number in (NEVER_PRS | limits["never_prs"]):', "if False:")]),
    # automerge_gate.py: the hold-on-findings window
    ("ag_window_start", AG, [("inside = stamp is None or parse_time(stamp) >= start",
                              "inside = stamp is None")]),
    ("ag_window_start_inclusive", AG, [("parse_time(stamp) >= start", "parse_time(stamp) > start")]),
    ("ag_window_skips_merges", AG, [('        if len(c["parents"]) == 1:\n', "        if True:\n")]),
    ("ag_old_sha_review_counts", AG, [("inside = stamp is None or parse_time(stamp) >= start",
                                       'inside = (stamp is None or parse_time(stamp) >= start) and '
                                       '(kind != "review" or item.get("commit_id") == head)')]),
    ("ag_head_review_inside", AG, [('if kind == "review" and item.get("commit_id") == head:\n            inside = True',
                                    'if kind == "review" and item.get("commit_id") == head:\n            pass')]),
    ("ag_unknown_time_inside", AG, [("inside = stamp is None or parse_time(stamp) >= start",
                                     "inside = stamp is not None and parse_time(stamp) >= start")]),
    ("ag_issue_comments_scanned", AG, [('(("issue_comment", issue_comments), ("review_comment", review_comments))',
                                        '(("review_comment", review_comments),)')]),
    ("ag_review_comments_scanned", AG, [('(("issue_comment", issue_comments), ("review_comment", review_comments))',
                                         '(("issue_comment", issue_comments),)')]),
    ("ag_comment_updated_at", AG, [('for s in (c.get("created_at"), c.get("updated_at")) if s]',
                                    'for s in (c.get("created_at"),) if s]')]),
    ("ag_ours_skipped", AG, [('        if login in ids["ours"]:\n            continue', "        if False:\n            continue")]),
    ("ag_bot_needs_read_ignored", AG, [('(verdict == "needs_read" and is_bot(item, ids))', "False")]),
    ("ag_bot_type", AG, [('(item.get("user") or {}).get("type") == "Bot" or ', "")]),
    ("ag_bot_logins", AG, [(' or login in ids["bots"]\n', "\n")]),
    ("ag_bot_suffixes", AG, [("\n            or any(login.endswith(s) for s in ids[\"bot_suffixes\"]))", ")")]),
    ("ag_findings_hold", AG, [("    if findings or needs:\n", "    if False:\n")]),
    ("ag_needs_read", AG, [('    return "needs_read"', '    return "clean"')]),
    ("ag_clean_bodies", AG, [('if text.lower().rstrip(".!").strip() in CLEAN_BODIES:', "if False:")]),
    ("ag_urgent_when_armed", AG, [('urgent = "URGENT auto-merge already armed; " if armed else ""', 'urgent = ""')]),
    ("ag_inputs_in_sync", AG, [('    if commits[-1]["sha"] != head:\n', "    if False:\n")]),
    ("ag_pages_concatenated", AG, [("rows = [x for v in values for x in v]", "rows = values[0]")]),
    ("ag_pages_slurp_flattened", AG, [("if rows and all(isinstance(x, list) for x in rows):  # --slurp", "if False:  # --slurp")]),
    ("ag_identities_validated", AG, [("    if not ours:\n", "    if False:\n")]),
    # automerge_gate.py: persisted holds
    ("ag_hold_persisted", AG, [("        if not newer and not read_clear:\n", "        if False:\n")]),
    ("ag_hold_needs_newer_commit", AG, [('newer = held["sha"] != anchor["sha"] and anchor["at"] > parse_time(held["date"])',
                                         'newer = held["sha"] != anchor["sha"]')]),
    ("ag_hold_written", AG, [("        if change is not None:\n", "        if False:\n")]),
    ("ag_hold_cleared", AG, [("            holds.pop(str(number), None)", "            pass")]),
    # automerge_gate.py: approvals and checks
    ("ag_author_reviews_ignored", AG, [('others = sorted((r for r in reviews if login_of(r) not in ids["ours"] and r.get("state") != "PENDING"),',
                                        'others = sorted((r for r in reviews if r.get("state") != "PENDING"),')]),
    ("ag_review_decision_approved", AG, [('elif pr["reviewDecision"] != "APPROVED":', "elif False:")]),
    ("ag_review_decision_cr", AG, [('if pr["reviewDecision"] == "CHANGES_REQUESTED":', "if False:")]),
    ("ag_reviewer_cr", AG, [('if r.get("state") == "CHANGES_REQUESTED":', "if False:")]),
    ("ag_reviewer_not_approved", AG, [('elif r.get("state") != "APPROVED":', "elif False:")]),
    ("ag_approval_on_head", AG, [('elif r.get("commit_id") not in covered:', "elif False:")]),
    ("ag_no_approval", AG, [("    if not latest:\n", "    if False:\n")]),
    ("ag_latest_non_commented", AG, [('if r.get("state") in ("COMMENTED", "PENDING"):', 'if r.get("state") in ("PENDING",):')]),
    ("ag_sorted_by_time", AG, [('                    key=lambda r: parse_time(r.get("submitted_at")))', "                    key=lambda r: 0)")]),
    ("ag_no_checks", AG, [("    if not rollup:\n", "    if False:\n")]),
    ("ag_failed_checks", AG, [("    if failed:\n", "    if False:\n")]),
    ("ag_pending_checks", AG, [("    if pending:\n", "    if False:\n")]),
    ("ag_ok_conclusions", AG, [('elif c.get("conclusion") not in OK_CONCLUSIONS:', "elif False:")]),
    ("ag_completed_needs_conclusion", AG, [('if c.get("status") != "COMPLETED" or not c.get("conclusion"):',
                                            'if c.get("status") != "COMPLETED":')]),
    ("ag_status_context_failed", AG, [("(pending if state in PENDING_STATES else failed)",
                                       "(pending if state in PENDING_STATES else [])")]),
    # automerge_gate.py: required contexts and merge state
    ("ag_required_missing", AG, [("    if absent:\n", "    if False:\n")]),
    ("ag_required_empty", AG, [("    if not required:\n", "    if False:\n")]),
    ("ag_required_rules", AG, [('            out |= _contexts(params["required_status_checks"], "rules_checks")', "            pass")]),
    ("ag_required_classic_contexts", AG, [('            out |= _contexts(part["contexts"], "classic_contexts")', "            pass")]),
    ("ag_required_classic_checks", AG, [('            out |= _contexts(part["checks"], "classic_checks")', "            pass")]),
    ("ag_merge_state_checked", AG, [("if state not in MERGE_STATE_OK[action]:", 'if state == "DIRTY":')]),
    # arm on BEHIND; update-branch keeps the approval, a fix push doesn't
    ("ag_arm_on_behind", AG, [('"arm": {"CLEAN", "HAS_HOOKS", "BEHIND"}', '"arm": {"CLEAN", "HAS_HOOKS"}')]),
    ("ag_approval_covers_main_merges", AG, [('elif r.get("commit_id") not in covered:',
                                             'elif r.get("commit_id") != head:')]),
    ("ag_approval_stale_after_fix_push", AG, [("    for j in range(len(commits) - 1, anchor[\"index\"] - 1, -1):",
                                               "    for j in range(len(commits) - 1, -1, -1):"),
                                              ("        if j == anchor[\"index\"] or not is_main_merge(commits[j]):",
                                               "        if False:")]),
    ("ag_update_on_behind", AG, [('"update_branch": {"BEHIND"}', '"update_branch": set()')]),
    ("ag_only_main_merges_carry", AG, [("bool(MAIN_MERGE.match(message[0]))", "True")]),
    # automerge_gate.py: update_branch versus arm (findings 2 + 11)
    ("ag_update_needs_behind", AG, [('"update_branch": {"BEHIND"}', '"update_branch": {"BEHIND", "CLEAN"}')]),
    ("ag_update_allowed_when_armed", AG, [('if action == "arm" and armed:', "if armed:")]),
    ("ag_already_armed", AG, [('if action == "arm" and armed:', "if False:")]),
    ("ag_update_word", AG, [('word = "ARM" if action == "arm" else "UPDATE_BRANCH"', 'word = "ARM"')]),
    ("ag_size_cap", AG, [('if lines > limits["cap"]:', 'if lines > limits["cap"] + 1:')]),
    ("ag_cap_from_mandate", AG, [('cap = cond.get("max_changed_lines", DEFAULT_CAP)', "cap = DEFAULT_CAP")]),
    ("ag_scope_from_mandate", AG, [("    if not author or not isinstance(repo, str) or not repo:\n        raise BadInput",
                                    "    if False:\n        raise BadInput")]),
    # automerge_gate.py: clear-by-reading bound to the exact body
    ("ag_cleared_skip", AG, [("        if is_cleared(kind, item, cleared):\n            continue",
                              "        if False:\n            continue")]),
    ("ag_cleared_needs_body_sha", AG, [("return ((item_key(kind, item), body_sha256(body)) in cleared and",
                                        "return (item_key(kind, item) in {k for k, _ in cleared} and")]),
    ("ag_cleared_never_real", AG, [("            and not real_findings(body))", "            and True)")]),
    ("ag_cleared_per_pr", AG, [('for r in holds.get(CLEARED, []) if r["pr"] == str(number)}', 'for r in holds.get(CLEARED, []) if True}')]),
    ("ag_persisted_read_clear", AG, [("        if not newer and not read_clear:", "        if not newer:")]),
    ("ag_persisted_all_items", AG, [('read_clear = all(persisted_item_cleared(i, current, cleared) for i in held["items"])',
                                     'read_clear = any(persisted_item_cleared(i, current, cleared) for i in held["items"])')]),
    ("ag_persisted_not_real", AG, [('    return (rec["id"], body_sha256(body)) in cleared and not real_findings(body)',
                                    '    return (rec["id"], body_sha256(body)) in cleared')]),
    ("ag_real_nit_lines", AG, [('        return bool(m.group("close")) or rest.startswith(SEPARATORS)', "        return False")]),
    ("ag_classic_404_ignored", AG, [('    if classic["http_status"] == 404:\n', '    if classic["http_status"] == -1:\n')]),
    # --- real versus historical findings; recompute from the current body ---
    ("ag_d1_nonzero_count", AG, [("    if NONZERO_COUNT.search(text):\n        return True", "    if False:\n        return True")]),
    ("ag_d1_line_tag", AG, [("        if line_tagged(line):\n            return True", "        if False:\n            return True")]),
    ("ag_d1_heading_list", AG, [("        elif in_findings and LIST_ITEM.match(line):", "        elif False:")]),
    ("ag_d1_zero_count_is_no_item", AG, [(r'    return not re.match(r"^[:=][ \t]*0+(?![0-9])", rest)', "    return True")]),
    ("ag_d1_prose_mention_not_real", AG, [("    if NONZERO_COUNT.search(text):\n",
                                          '    if NONZERO_COUNT.search(text) or re.search(r"\\bP[0-3]\\b", text):\n')]),
    ("ag_d1_fence_headings", AG, [("h = None if in_fence else HEADING.match(line)", "h = HEADING.match(line)")]),
    ("ag_d1_trust_persisted_real", AG, [('    return (rec["id"], body_sha256(body)) in cleared and not real_findings(body)',
                                         '    return rec.get("real") is False and (rec["id"], rec.get("sha256")) in cleared')]),
    ("ag_d1_gone_saved_body", AG, [('    elif body_sha256(rec["body"]) == rec["sha256"]:', "    elif False:")]),
    ("ag_d1_saved_body_sha", AG, [('    elif body_sha256(rec["body"]) == rec["sha256"]:', "    elif True:")]),
    ("ag_d1_body_saved", AG, [('                     "body": normalise_body(item.get("body"))})', "                     })")]),
    ("hd_vacuity_clean_clear_errors", HD, [("    try:\n        with open(a.mandates) as fh:",
                                            '    return error("mutant: clean bodies error")\n    try:\n        with open(a.mandates) as fh:')]),
    # --- one-offs are GO records, checked in full ---
    ("cm_d2_oneoff_needs_user", CM, [('    return go is not None and go["granted_by"] == "user"', "    return go is not None")]),
    ("cm_d2_oneoff_allows_forbidden", CM, [('        return oneoff or ("DENY", f"forbidden_without_user:{action}")',
                                            '        return ("DENY", f"forbidden_without_user:{action}")')]),
    ("cm_d2_go_expiry", CM, [("    if now >= expires:\n", "    if False:\n")]),
    ("cm_d2_go_repo", CM, [('    want = {"action": action, "repo": repo,', '    want = {"action": action,')]),
    ("cm_d2_go_branch", CM, [('            "branch": extra.get("branch"), "head": extra["expect_head"],', '            "head": extra["expect_head"],')]),
    ("gl_d2_relayed_by", GL, [('    if a.granted_by == "user" and not (a.relayed_by or "").strip():', "    if False:")]),
    ("gl_d2_expiry_range", GL, [("    if not 1 <= a.expires_in <= 1440:", "    if False:")]),
    ("gl_d2_expiry_written", GL, [('"expires_at": (now + timedelta(minutes=a.expires_in)).isoformat(timespec="seconds"),',
                                   '"expires_at": (now + timedelta(days=9999)).isoformat(timespec="seconds"),')]),
    ("gl_d2_needs_target", GL, [("    if (a.pr is None and a.branch is None) or", "    if False or")]),
    # --- the push publishes exactly the GO's new_head ---
    ("cm_d3_pin_required", CM, [("    if is_push(action) and pre_write and (new is None or loc is None or not br):", "    if False:")]),
    ("cm_d3_local_moved", CM, [("        if new != loc:\n", "        if False:\n")]),
    ("cm_d3_go_new_head", CM, [('            "new_head": extra.get("new_head"), "body_sha256"', '            "new_head": go.get("new_head"), "body_sha256"')]),
    ("cm_d3_pin_sha_format", CM, [("        if not SHA_RE.fullmatch(new) or not SHA_RE.fullmatch(loc):", "        if False:")]),
    ("gl_d3_push_needs_new_head", GL, [("        if a.new_head is None or not SHA_RE.fullmatch(a.new_head) or a.branch is None:", "        if False:")]),
    # --- required-context provenance ---
    ("ag_d4_rules_200", AG, [('    if rules["http_status"] != 200:', "    if False:")]),
    ("ag_d4_rules_parameters", AG, [('            if not isinstance(params, dict) or "required_status_checks" not in params:\n                raise Unverified("rules_parameters")',
                                     '            if not isinstance(params, dict):\n                params = {}\n            if "required_status_checks" not in params:\n                continue')]),
    ("ag_d4_classic_404_message", AG, [('        if not (isinstance(body, dict) and body.get("message") == "Branch not protected"):', "        if False:")]),
    ("ag_d4_classic_other_status", AG, [("        raise Unverified(f\"classic_status={classic['http_status']}\")", "        pass")]),
    ("ag_d4_both_envelopes", AG, [('    for name in ("rules", "classic"):', "    for name in ():")]),
    ("ag_d4_envelope_shape", AG, [('        if not isinstance(env, dict) or set(env) != {"endpoint", "http_status", "body"}:',
                                   '        if not isinstance(env, dict):')]),
    ("ag_d4_status_int", AG, [("        if not strict_int(env[\"http_status\"], 100):", "        if False:")]),
    ("ag_d4_unverified_waits", AG, [('        waits.append(f"required_contexts_unverified:{exc}")', "        required = set()")]),
    ("ae_d4_next_link", AE, [("""        if name.strip().lower() == "link" and re.search(r'rel="next"', value):""", "        if False:")]),
    ("ae_d4_status_line", AE, [("    if not m:\n        return None, None", "    if not m:\n        return 200, json.loads(body or 'null')")]),
    # --- no stealing a live reservation ---
    ("ob_d5_failed_needs_owner", OB, [("            if how is None:\n", "            if False:\n")]),
    ("ob_d5_gone_evidence_exact", OB, [('    if evidence != SS.gone_evidence(owner):', '    if not evidence.endswith(" 404"):')]),
    ("ob_d5_token_checked", OB, [('        if not re.fullmatch(r"[0-9a-f]{32}", token) or \\\n', "        if False and \\\n")]),
    ("ob_d5_owner_validated", OB, [("            if not TERMINAL_RE.fullmatch(a.owner_terminal):", "            if False:")]),
    ("ob_d5_owner_recorded", OB, [('"owner_terminal": a.owner_terminal, "generation": generation,', '"owner_terminal": "aaaa1111", "generation": generation,')]),
    ("ob_d5_token_hashed", OB, [('"token_sha256": hashlib.sha256(token.encode()).hexdigest()}', '"token_sha256": token, "token": token}')]),
    # --- strict scalars ---
    ("ag_d6_number_strict", AG, [('    if not strict_int(pr["number"], 1):', "    if False:")]),
    ("ag_d6_counts_strict", AG, [("        if not strict_int(pr[k], 0):", "        if False:")]),
    ("ag_d6_no_bools", AG, [("return isinstance(value, int) and not isinstance(value, bool) and value >= minimum",
                             "return isinstance(value, int) and value >= minimum")]),
    ("ag_d6_positive_number", AG, [('    if not strict_int(pr["number"], 1):', '    if not strict_int(pr["number"], 0):')]),
    # --- the docs and the templates: doc mutants (the copy carries every doc) ---
    ("doc_d7_outbox_record", MJ, [('outbox.py sent <key> --url <url>"', 'outbox.py record <key> --url <url>"')]),
    ("doc_d2_reduced_path", PUB, [("**Push pin:**", "**A conductor one-off** uses the same steps, with each `check_mandate.py` line replaced by "
                                   "`python3 -c 'import json,sys; sys.exit(json.load(open(\"m\"))[\"paused\"] is not False)'`.\n**Push pin:**")]),
    ("doc_d3_bare_push", PUB, [("5. git -C {worktree} push {push_url} {new}:refs/heads/{branch}", "5. git -C {worktree} push {push_url} {branch}")]),
    ("doc_d3_pin_dropped", PUB, [(' --new-head {new} --local-head "$L"', "")]),
    ("doc_d4_no_status", SPB, [("gh api -i $R/branches/main/protection >", "gh api $R/branches/main/protection >")]),
    ("doc_d5_steal", PUB, [("R6. python3 $K/outbox.py failed {key} --reason search_found_no_post --owner-gone --generation",
                            "R6. python3 $K/outbox.py failed {key} --reason search_found_no_post --token none --generation")]),
    # --- only the conductor writes go.jsonl ---
    ("gl_v31_writer_role_refused", GL, [('    if a.writer_role != "conductor":', "    if False:")]),
    ("gl_v31_writer_terminal_required", GL, [("    if not TERMINAL_RE.fullmatch(writer):", "    if False:")]),
    ("gl_v31_writer_recorded", GL, [('"revoked": False, **signed}', '"revoked": False}')]),
    # check_mandate.py: clear_hold always needs GO
    ("cm_go_always_required", CM, [("if go_always and go is None:", "if False:")]),
    ("gl_item_recorded", GL, [('        row["item"] = a.item', "        pass")]),
    # holds.py
    ("hd_go_missing", HD, [("    if go is None:\n        return refuse", "    if False:\n        return refuse")]),
    ("hd_go_latest_wins", HD, [("return hits[-1] if hits else None", "return hits[0] if hits else None")]),
    ("hd_real_findings_refused", HD, [("    if GATE.real_findings(text):", "    if False:")]),
    ("hd_body_canonical", HD, [("    if GATE.normalise_body(text) != text:", "    if False:")]),
    ("hd_mandate_required", HD, [("    if not mandates:\n", "    if False:\n")]),
    ("hd_reason_one_line", HD, [('if not reason or "\\n" in a.reason or len(reason) > 200:', "if False:")]),
    # outbox.py
    ("ob_used_blocks", OB, [('    row = sent_row(key)\n    if row is not None:\n        return "USED", row',
                             '    row = sent_row(key)\n    if False:\n        return "USED", row')]),
    ("ob_pending_blocks_check", OB, [('    if os.path.exists(entry(key, "pending")):\n        return "PENDING", None',
                                      '    if False:\n        return "PENDING", None')]),
    ("ob_reserve_excl", OB, [("os.O_WRONLY | os.O_CREAT | os.O_EXCL", "os.O_WRONLY | os.O_CREAT | os.O_TRUNC")]),
    ("ob_reserve_used", OB, [("                row = sent_row(a.key)  # a corrupt .sent or history raises: ERROR, nothing reserved\n                if row is not None:",
                               "                row = sent_row(a.key)  # a corrupt .sent or history raises: ERROR, nothing reserved\n                if False:")]),
    ("ob_reserve_rederives_key", OB, [("if make_key(a.pr, a.kind, a.head, body) != a.key:", "if False:")]),
    ("ob_lock_held", OB, [("        fcntl.flock(self.fd, fcntl.LOCK_EX)\n", "        pass\n")]),
    ("ob_sent_needs_reservation", OB, [('                pending = entry(a.key, "pending")\n                if not os.path.exists(pending):\n                    print(f"NOT_RESERVED {a.key} url',
                                        '                pending = entry(a.key, "pending")\n                if False:\n                    print(f"NOT_RESERVED {a.key} url')]),
    ("ob_sent_duplicate", OB, [("                if prior is not None:\n", "                if False:\n")]),
    ("ob_failed_needs_reservation", OB, [('            if not os.path.exists(pending):\n                print(f"NOT_RESERVED {a.key}")\n',
                                          '            if False:\n                print(f"NOT_RESERVED {a.key}")\n')]),
    ("ob_history_row_counts", OB, [("        pass\n    return find(key)\n", "        pass\n    return None\n")]),
    ("ob_find_post_login", OB, [('hits = [c for c in rows if c["user"]["login"] == login\n            and ',
                                 "hits = [c for c in rows if True\n            and ")]),
    ("ob_find_post_exact_body", OB, [("and normalise(c[\"body\"]) == want]", "and True]")]),
    ("ob_find_post_many", OB, [("    if len(hits) > 1:\n", "    if False:\n")]),
    ("ob_key_pr", OB, [('{"pr": str(pr),', '{"pr": "",')]),
    ("ob_key_kind", OB, [('"kind": kind,', '"kind": "",')]),
    ("ob_key_head", OB, [('"head": head,', '"head": "",')]),
    ("ob_key_body", OB, [('"body": body}', '"body": ""}')]),
    ("ob_normalise_body", OB, [('body = body.replace("\\r\\n", "\\n").rstrip("\\n")', "body = body")]),
    ("ob_key_format", OB, [("if not KEY_RE.fullmatch(a.key):", "if False:")]),
    # ---
    # the push destination is bound to the repo's authorised URL (check_mandate.py + template)
    ("cm_f1_url_bound", CM, [("    if not isinstance(want, str) or not want.strip():\n        return f\"push_url_unbound", "    if False:\n        return f\"push_url_unbound")]),
    ("cm_f1_repo_lookup", CM, [("want = urls.get(repo) if", "want = urls.get(DEFAULT_REPO) if")]),
    ("cm_f1_needs_all", CM, [("    if dest is None or rewrites is None or None in observed.values():", "    if dest is None:")]),
    ("cm_f1_rewrites", CM, [("    if rewrites.strip():\n", "    if False:\n")]),
    ("cm_f1_url_count", CM, [("        if len(lines) != 1:\n", "        if False:\n")]),
    ("cm_f1_url_match", CM, [("        if norm_url(lines[0]) != norm_url(want):", "        if False:")]),
    ("cm_f1_fetch_checked", CM, [('observed = {"push_url": extra.get("push_url"), "fetch_url": extra.get("fetch_url")}',
                                  'observed = {"push_url": extra.get("push_url")}')]),
    ("cm_f1_dest_match", CM, [("    if norm_url(dest) != norm_url(want):", "    if False:")]),
    ("cm_f1_norm_needed", CM, [('    return url[:-4] if url.endswith(".git") else url', "    return url")]),
    ("cm_f1_norm_only_git", CM, [('    return url[:-4] if url.endswith(".git") else url',
                                  '    url = url.rstrip("/").lower()\n    return url[:-4] if url.endswith(".git") else url')]),
    ("cm_f1_push_pre_write", CM, [("    if is_push(action) and pre_write:\n        return check_destination", "    if False:\n        return check_destination")]),
    ("cm_f1_repeated_joined", CM, [('else "\\n".join(v)', "else v[-1]")]),
    ("doc_f1_push_origin", PUB, [("5. git -C {worktree} push {push_url} {new}", "5. git -C {worktree} push origin {new}")]),
    ("doc_f1_ls_remote_origin", PUB, [("R=$(git -C {worktree} ls-remote {push_url} refs/heads/{branch} | cut -f1); L=",
                                       "R=$(git -C {worktree} ls-remote origin refs/heads/{branch} | cut -f1); L=")]),
    ("doc_f1_pushurl_not_passed", PUB, [(' --push-url "$PU"', "")]),
    ("doc_f1_rewrites_not_read", PUB, [("RW=$(git -C {worktree} config --get-regexp '^url\\.')", 'RW=""')]),
    # holds.py clear runs the full check_mandate decision
    ("hd_f2_decision_enforced", HD, [('    if word != "ALLOW":\n', "    if False:\n")]),
    ("hd_f2_real_target", HD, [('CM.decide(doc, "shepherd", "clear_hold", a.pr_author, a.base, a.repo, extra, records)',
                                'CM.decide(doc, "shepherd", "clear_hold", "your-login", "main", "your-org/your-repo", extra, records)')]),
    ("hd_f2_go_id_passed", HD, [('extra = {"go_id": a.go_id,', 'extra = {"go_id": None,')]),
    ("hd_f2_pr_passed", HD, [('"pr": a.pr, "body_sha256": sha,', '"pr": go.get("pr"), "body_sha256": sha,')]),
    ("hd_f2_body_passed", HD, [('"body_sha256": sha, "expect_head"', '"body_sha256": go.get("body_sha256"), "expect_head"')]),
    ("hd_f2_item_on_top", HD, [('    if go.get("item") != a.item:', "    if False:")]),
    # reconcile after the owner-gone proof, with a fresh, later search, for the same generation
    ("ob_f3_generation_increments", OB, [("generation = read_generation(a.key) + 1", "generation = 1")]),
    ("ob_f3_generation_recorded", OB, [("                write_generation(a.key, generation)\n", "                pass\n")]),
    ("ob_f3_proof_generation", OB, [("    if generation != gen:\n", "    if False:\n")]),
    ("ob_f3_proof_recorded", OB, [('    SS.append_row(entry(key, "pending"), proof, SS.pending_append_check(key))\n', "    pass\n")]),
    ("ob_f3_release_generation", OB, [("    if a.generation != gen:\n", "    if False:\n")]),
    ("ob_f3_proof_required", OB, [("    if not proofs:\n", "    if False:\n")]),
    ("ob_f3_latest_proof", OB, [("proven = max(", "proven = min(")]),
    ("ob_f3_fetched_after_proof", OB, [("    if not fetched > proven:\n", "    if False:\n")]),
    ("ob_f3_written_after_proof", OB, [("    if not written > proven:\n", "    if False:\n")]),
    ("ob_f3_fetched_zone", OB, [("    if dt.tzinfo is None:\n        return None", "    if False:\n        return None")]),
    ("ob_f3_body_is_the_key", OB, [('    if make_key(row["pr"], row["kind"], row["head"], body) != key:', "    if False:")]),
    ("ob_f3_search_unreadable", OB, [('    if err:\n        return None, f"search_unreadable', '    if False:\n        return None, f"search_unreadable')]),
    ("ob_f3_found_refuses", OB, [('    if hits:\n        return None, f"search_found_post', '    if False:\n        return None, f"search_found_post')]),
    ("ob_f3_sent_generation", OB, [("                if a.generation is not None and a.generation != gen:", "                if False:")]),
    ("ob_f3_release_needs_inputs", OB, [("if a.owner_gone and None in (", "if False and None in (")]),
    ("doc_f3_search_first", PUB, [("R1. python3 $K/outbox.py check {key}",
                                   "R1. gh api --paginate --slurp repos/your-org/your-repo/issues/{pr}/comments > {search_file}; python3 $K/outbox.py check {key}")]),
    ("doc_f3_no_fetch_time", PUB, [("R4. python3 $K/outbox.py now > {fetched_file}; gh api", "R4. gh api")]),
    ("doc_f3_sent_any_generation", PUB, [("sent {key} --url <url> --generation {generation}", "sent {key} --url <url>")]),
    # resource_gate.py: thresholds
    ("rg_stop_pressure", RG, [('if p >= st["pressure_level_gte"]:', 'if p > st["pressure_level_gte"]:')]),
    ("rg_stop_free_mem", RG, [('if f < st["free_memory_pct_lt"]:', 'if f <= st["free_memory_pct_lt"]:')]),
    ("rg_stop_disk", RG, [('if d < st["disk_free_gib_lt"]:', 'if d <= st["disk_free_gib_lt"]:')]),
    ("rg_stop_terminals", RG, [('if t >= st["live_terminals_gte"]:', 'if t > st["live_terminals_gte"]:')]),
    ("rg_throttle_pressure", RG, [('elif p >= th["pressure_level_gte"]:', 'elif p > th["pressure_level_gte"]:')]),
    ("rg_throttle_free_mem", RG, [('elif f < th["free_memory_pct_lt"]:', 'elif f <= th["free_memory_pct_lt"]:')]),
    ("rg_throttle_swap", RG, [('s > th["swap_used_gib_gt"]:', 's >= th["swap_used_gib_gt"]:')]),
    ("rg_throttle_disk", RG, [('elif d < th["disk_free_gib_lt"]:', 'elif d <= th["disk_free_gib_lt"]:')]),
    ("rg_throttle_load", RG, [('load > th["load_per_cpu_gt"]:', 'load >= th["load_per_cpu_gt"]:')]),
    ("rg_load_per_cpu", RG, [('load = m["load1"] / m["ncpu"]', 'load = m["load1"] / 10')]),
    ("rg_throttle_terminals", RG, [('elif t >= th["live_terminals_gte"]:', 'elif t > th["live_terminals_gte"]:')]),
    ("rg_unmeasured_throttles", RG, [('throttle.append(f"unmeasured:{name}")', "pass")]),
    ("rg_cao_error_throttles", RG, [("stop, throttle = [], list(errors)", "stop, throttle = [], []")]),
    ("rg_cao_error_no_crash", RG, [("    except Exception as exc:\n        return None, f\"cao_api:",
                                    "    except ImportError as exc:\n        return None, f\"cao_api:")]),
    ("rg_terminals_all_sessions", RG, [("total += len(terms)", "total = len(terms)")]),
    ("rg_stop_wins", RG, [("    if stop:\n", "    if False:\n")]),
    ("rg_fake_means_no_probes", RG, [("    if fake:\n", "    if False:\n")]),
    # resource_gate.py: validation and live parsers
    ("rg_pressure_parser_const", RG, [("def _pressure(cfg):\n    return parse_pressure(",
                                       "def _pressure(cfg):\n    return 1 if True else parse_pressure(")]),
    ("rg_pressure_parser_strict", RG, [('if not re.fullmatch(r"-?[0-9]+", t):', "if not t:")]),
    ("rg_pressure_valid_set", RG, [("VALID_PRESSURE = (1, 2, 4)", "VALID_PRESSURE = (0, 1, 2, 3, 4)")]),
    ("rg_pressure_must_be_int", RG, [("isinstance(p, bool) or not isinstance(p, int) or p not in VALID_PRESSURE",
                                      "p not in VALID_PRESSURE")]),
    ("rg_finite_and_range", RG, [("ok = ok and math.isfinite(v) and lo <= v <= hi", "ok = ok")]),
    ("rg_numbers_not_bools", RG, [("ok = isinstance(v, (int, float)) and not isinstance(v, bool)",
                                   "ok = isinstance(v, (int, float))")]),
    ("rg_int_keys", RG, [("        if ok and key in INT_KEYS:\n", "        if False:\n")]),
    ("rg_invalid_throttles", RG, [("list(errors) + problems", "list(errors)")]),
    ("rg_probe_path", RG, [('search = os.environ.get("SUPER_AGENT_PROBE_PATH") or (', "search = None or (")]),
    ("rg_probe_timeout", RG, [('return float((cfg.get("probe_timeouts") or {}).get(tool, DEFAULT_TIMEOUTS[tool]))',
                               "return float(DEFAULT_TIMEOUTS[tool])")]),
    ("rg_swap_units", RG, [('{"K": 1024 ** 2, "M": 1024, "G": 1}', '{"K": 1024 ** 2, "M": 1000, "G": 1}')]),
    ("rg_free_parser", RG, [('m = re.search(r"free percentage:', 'm = re.search(r"free percentagez:')]),
    # resource_gate.py: heavy slot
    ("rg_heavy_refused_on_stop", RG, [('if verdict == "STOP":', "if False:")]),
    ("rg_heavy_one_at_a_time", RG, [("        if os.path.isdir(marker):\n            owner = _owner(marker)",
                                     "        if False:\n            owner = _owner(marker)"),
                                    ("        os.mkdir(marker)\n", "        os.makedirs(marker, exist_ok=True)\n")]),
    ("rg_heavy_stale_reported", RG, [("            if age > stale_s:\n", "            if False:\n")]),
    ("rg_stale_from_last_refresh", RG, [('\n            age = time.time() - owner["refreshed_at"]',
                                         '\n            age = time.time() - owner["acquired_at"]')]),
    ("rg_lifecycle_lock", RG, [("                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n",
                                "                pass\n")]),
    ("rg_lock_token", RG, [("return isinstance(held, str) and secrets.compare_digest(held, token)", "return True")]),
    ("rg_token_unique", RG, [("token = secrets.token_hex(16)", 'token = "0" * 32')]),
    ("rg_token_validated", RG, [("if token is not None and not TOKEN_RE.fullmatch(token):", "if False:")]),
    ("rg_refresh_updates", RG, [('        owner["refreshed_at"] = time.time()\n', "        pass\n")]),
    ("rg_refresh_stale_refused", RG, [("\n        if age > stale_s:\n            return 1, (f\"REFUSED STALE",
                                       "\n        if False:\n            return 1, (f\"REFUSED STALE")]),
    ("rg_release_not_held", RG, [("            return 1, \"NOT_HELD\"\n        owner = _owner(marker)\n        if not _is_owner(owner, token):\n            return 1, f\"REFUSED not_owner held_by={owner.get('label')}\"\n        os.remove",
                                  "            pass\n        owner = _owner(marker)\n        if not _is_owner(owner, token):\n            return 1, f\"REFUSED not_owner held_by={owner.get('label')}\"\n        os.remove")]),
    ("rg_label_validated", RG, [("if a.acquire_heavy is not None and not LABEL_RE.fullmatch(a.acquire_heavy):", "if False:")]),
    # plan_hash_check.py
    ("ph_missing_baseline_refused", PH, [("        if want is None:\n", "        if False:\n")]),
    ("ph_changed_refused", PH, [("        elif got != want:\n", "        elif False:\n")]),
    ("ph_only_baselines_trusted", PH, [('        if row["kind"] != "baseline":\n            continue', '        if False:\n            continue')]),
    ("ph_latest_baseline_wins", PH, [('        out[row["file"]] = row["sha256"]\n', '        out.setdefault(row["file"], row["sha256"])\n')]),
    ("ph_exit_code", PH, [("return 0 if ok else 1", "return 0")]),
    # remap_owners.py
    ("ro_only_the_old_owner", RO, [('if isinstance(owner, dict) and owner.get("terminal") == old:',
                                    "if isinstance(owner, dict):")]),
    ("ro_issues_too", RO, [('for group in ("prs", "issues"):', 'for group in ("prs",):')]),
    ("ro_identities_added", RO, [('        doc["identities"] = {"ours": list(ours), **BOT_IDENTITIES}', "        pass")]),
    ("ro_identities_kept", RO, [('    if "identities" not in doc:', "    if True:")]),
    ("ro_identities_need_ours", RO, [("        if not ours:\n", "        if False:\n")]),
    # outbox search rows + the strictness sweep
    ("ob_f4_rows_checked", OB, [("        why = bad_comment(c)\n        if why:\n", "        why = bad_comment(c)\n        if False:\n")]),
    ("ob_f4_body_str", OB, [('    if not isinstance(c.get("body"), str):\n        return "a row has no string body"',
                             '    if False:\n        return "a row has no string body"')]),
    ("ob_f4_user_object", OB, [('    if not isinstance(user, dict):\n        return "a row has no user object"',
                                '    if False:\n        return "a row has no user object"')]),
    ("ob_f4_login_str", OB, [('    if not isinstance(user.get("login"), str) or not user["login"].strip():', "    if False:")]),
    ("ob_f4_login_nonblank", OB, [('    if not isinstance(user.get("login"), str) or not user["login"].strip():',
                                   '    if not isinstance(user.get("login"), str):')]),
    ("ob_f4_url_str", OB, [('    if not isinstance(c.get("html_url"), str) or not c["html_url"].strip():', "    if False:")]),
    ("ob_f4_url_nonblank", OB, [('    if not isinstance(c.get("html_url"), str) or not c["html_url"].strip():',
                                 '    if not isinstance(c.get("html_url"), str):')]),
    ("ob_f4_login_arg", OB, [('    if not isinstance(login, str) or not login.strip() or re.search(r"\\s", login):', "    if False:")]),
    ("ob_f4_login_arg_spaces", OB, [('    if not isinstance(login, str) or not login.strip() or re.search(r"\\s", login):',
                                     "    if not isinstance(login, str) or not login.strip():")]),
    ("ag_sw_items_checked", AG, [('        if name != "commits":\n', "        if False:\n")]),
    ("ag_sw_item_user", AG, [('    if not isinstance(user, dict) or not isinstance(user.get("login"), str) or not user["login"].strip():\n        return "user"',
                              '    if False:\n        return "user"')]),
    ("ag_sw_item_login_nonblank", AG, [(' or not user["login"].strip():\n        return "user"', ':\n        return "user"')]),
    ("ag_sw_item_body", AG, [('    if not isinstance(item.get("body"), str):\n        return "body"', '    if False:\n        return "body"')]),
    ("ag_sw_automerge_required", AG, [('"mergeStateStatus", "autoMergeRequest")', '"mergeStateStatus")')]),
    ("ag_sw_automerge_type", AG, [('    if pr["autoMergeRequest"] is not None and not isinstance(pr["autoMergeRequest"], dict):', "    if False:")]),
    ("ag_sw_review_time", AG, [('key=lambda r: parse_time(r.get("submitted_at")))', 'key=lambda r: r.get("submitted_at") or "")')]),
    ("ag_sw_pending_not_ordered", AG, [(' and r.get("state") != "PENDING"),', "),")]),
    ("ag_sw_conditions_object", AG, [("            if not isinstance(cond, dict):  # null or a list", "            if False:  # null or a list")]),
    ("ag_sw_never_prs_type", AG, [("            if not isinstance(listed, list) or not all(strict_int(n, 1) for n in listed):", "            if False:")]),
    ("ag_sw_cap_type", AG, [("                if not strict_int(cap, 0):", "                if False:")]),
    ("cm_sw_forbidden_list", CM, [('    if not isinstance(forbidden, list) or not all(isinstance(x, str) and x for x in forbidden):', "    if False:")]),
    ("cm_sw_forbidden_items", CM, [('    if not isinstance(forbidden, list) or not all(isinstance(x, str) and x for x in forbidden):',
                                    "    if not isinstance(forbidden, list):")]),
    ("cm_sw_go_always_bool", CM, [("            if not isinstance(go_always, bool):", "            if False:")]),
    ("cm_sw_conditions_object", CM, [('cond.get("go_always_required", False) if isinstance(cond, dict) else None',
                                      'cond.get("go_always_required", False) if True else None')]),
    ("hd_sw_body_str", HD, [('    if not isinstance(hits[0].get("body"), str):  # a null body', "    if False:  # a null body")]),
    ("rg_sw_session_name", RG, [("            if not isinstance(name, str) or not name.strip():  # never ask", "            if False:  # never ask")]),
    ("rg_sw_session_name_blank", RG, [("            if not isinstance(name, str) or not name.strip():  # never ask",
                                       "            if not isinstance(name, str):  # never ask")]),
    ("rg_sw_session_object", RG, [('name = s.get("name") or s.get("id") if isinstance(s, dict) else None',
                                   'name = s.get("name") or s.get("id") if True else None')]),
    # one strict validator per state-file row type (bin/state_schema.py)
    # -- the field checkers
    ("ss_text_empty", SS, [('        if not v.strip():\n            return "empty"', '        if False:\n            return "empty"')]),
    ("ss_text_one_line", SS, [('if len(v) > max_len or (one_line and ("\\n" in v or "\\r" in v)):', "if len(v) > max_len:")]),
    ("ss_text_max_len", SS, [('if len(v) > max_len or (one_line and ("\\n" in v or "\\r" in v)):', 'if (one_line and ("\\n" in v or "\\r" in v)):')]),
    ("ss_any_text_type", SS, [('    return None if isinstance(v, str) else "type"', "    return None")]),
    ("ss_pattern_format", SS, [('        return None if rx.fullmatch(v) else "format"', "        return None")]),
    ("ss_iso_parse", SS, [('    except ValueError:\n        return "format"', "    except ValueError:\n        return None")]),
    ("ss_iso_zone", SS, [('    return None if dt.tzinfo is not None else "no_zone"', "    return None")]),
    ("ss_integer_bool", SS, [("        if type(v) is not int:  # bool is not an int here", "        if not isinstance(v, int):")]),
    ("ss_integer_range", SS, [('        return None if v >= lo else "range"', "        return None")]),
    ("ss_number_bool", SS, [("        if isinstance(v, bool) or not isinstance(v, (int, float)):", "        if not isinstance(v, (int, float)):")]),
    ("ss_number_finite", SS, [('        return None if math.isfinite(v) and v >= lo else "range"', '        return None if v >= lo else "range"')]),
    ("ss_number_min", SS, [('        return None if math.isfinite(v) and v >= lo else "range"', '        return None if math.isfinite(v) else "range"')]),
    ("ss_const_type", SS, [('        if type(v) is not type(value):\n            return "type"', '        if False:\n            return "type"')]),
    ("ss_const_value", SS, [('        return None if v == value else "value"', "        return None")]),
    ("ss_boolean", SS, [('    return None if type(v) is bool else "type"', "    return None")]),
    ("ss_nullable", SS, [("        return None if v is None else chk(v)", "        return None")]),
    ("ss_one_of", SS, [("        return None if None in problems else problems[0]", "        return None")]),
    ("ss_map_items", SS, [("            if not key_rx.fullmatch(k) or value_chk(x):", "            if False:")]),
    ("ss_map_empty", SS, [('        if not v:\n            return "empty"\n        for k, x in v.items():', '        if False:\n            return "empty"\n        for k, x in v.items():')]),
    # -- a row type: required, optional, unknown fields and its cross-field check
    ("ss_row_not_object", SS, [('    def problem(self, row):\n        """None when the row is valid, else \'<field>:<problem>\'."""\n        if not isinstance(row, dict):',
                                '    def problem(self, row):\n        """None when the row is valid, else \'<field>:<problem>\'."""\n        if False:')]),
    ("ss_row_unknown_field", SS, [("            if field not in self.required and field not in self.optional:", "            if False:")]),
    ("ss_row_required_missing", SS, [('            if field not in row:\n                return f"{field}:missing"', "            if field not in row:\n                continue")]),
    ("ss_row_required_checked", SS, [('            why = chk(row[field])\n            if why:\n                return f"{field}:{why}"\n        for field, chk in self.optional',
                                      '            why = chk(row[field])\n            if False:\n                return f"{field}:{why}"\n        for field, chk in self.optional')]),
    ("ss_row_optional_checked", SS, [('                why = chk(row[field])\n                if why:', "                why = chk(row[field])\n                if False:")]),
    ("ss_row_cross_check", SS, [("        return self.check(row) if self.check else None", "        return None")]),
    # -- go.jsonl: grant and revoke rows
    ("ss_grant_target", SS, [('    if row["pr"] is None and row["branch"] is None:\n        return "target:missing"', '    if False:\n        return "target:missing"')]),
    ("ss_grant_push_new_head", SS, [('        if row["new_head"] is None or row["branch"] is None:', "        if False:")]),
    ("ss_grant_not_push", SS, [('    elif row["new_head"] is not None:', "    elif False:")]),
    ("ss_grant_user_relayed_by", SS, [('    if row["granted_by"] == "user" and "relayed_by" not in row:', "    if False:")]),
    ("ss_signed_writer_role", SS, [('SIGNED = {"writer_role": const("conductor"),', 'SIGNED = {"writer_role": text(),')]),
    ("ss_signed_writer_terminal", SS, [('"writer_terminal": pattern(TERMINAL_RE)}', '"writer_terminal": any_text}')]),
    ("ss_grant_expires_zoned", SS, [('"at": iso, "expires_at": iso,', '"at": iso, "expires_at": text(),')]),
    ("ss_go_row_object", SS, [('    if not isinstance(row, dict):\n        return "row:not_object"\n    revoked = row.get("revoked")',
                               '    if False:\n        return "row:not_object"\n    revoked = row.get("revoked")')]),
    ("ss_go_revoke_dispatch", SS, [("    if revoked is True:\n", "    if revoked is True and False:\n")]),
    ("ss_go_unknown_type", SS, [('    return "row_type:unknown (revoked must be true or false)"', "    return GO_GRANT.problem(row)")]),
    ("ss_f5_revoke_needs_grant", SS, [('        if why is None and row["id"] not in granted:', "        if False:")]),
    ("ss_f5_grants_tracked", SS, [('    granted = {r["id"] for r in rows if r["revoked"] is False}', "    granted = set()")]),
    ("ss_go_rows_raise", SS, [('        why = go_row_problem(row, *_go_ids(rows))\n        if why:\n            raise StateCorrupt',
                               '        why = go_row_problem(row, *_go_ids(rows))\n        if False:\n            raise StateCorrupt')]),
    ("ss_go_append_checked", SS, [('    why = go_row_problem(row, *_go_ids(rows))\n    if why:\n        raise RowRefused', '    why = go_row_problem(row, *_go_ids(rows))\n    if False:\n        raise RowRefused')]),
    # -- unique GO ids, one shared append routine
    ("r6_f8_grant_duplicate", SS, [('        if why is None and row["id"] in granted:\n            return "id:duplicate"', '        if False:\n            return "id:duplicate"')]),
    ("r6_f8_revoke_duplicate", SS, [('        if why is None and row["id"] in revoked_ids:', "        if False:")]),
    ("r6_f8_revoked_ids_tracked", SS, [('    revoked_ids = {r["id"] for r in rows if r["revoked"] is True}', "    revoked_ids = set()")]),
    ("r6_f8_load_tracks_ids", SS, [("        why = go_row_problem(row, *_go_ids(rows))\n        if why:\n            raise StateCorrupt", "        why = go_row_problem(row, set(), set())\n        if why:\n            raise StateCorrupt")]),
    ("r6_f8_append_tracks_ids", SS, [("    why = go_row_problem(row, *_go_ids(rows))\n    if why:\n        raise RowRefused", "    why = go_row_problem(row, set(), set())\n    if why:\n        raise RowRefused")]),
    ("r6_f9_append_checks_first", SS, [("    check(path, row)\n    try:", "    try:")]),
    ("r6_f9_sep_never", SS, [('    sep = b"\\n" if raw and not raw.endswith(b"\\n") else b""', '    sep = b""')]),
    ("r6_f9_sep_always", SS, [('    sep = b"\\n" if raw and not raw.endswith(b"\\n") else b""', '    sep = b"\\n"')]),
    ("r6_f9_sep_on_empty", SS, [('    sep = b"\\n" if raw and not raw.endswith(b"\\n") else b""', '    sep = b"\\n" if not raw.endswith(b"\\n") else b""')]),
    ("r6_f9_missing_is_empty", SS, [('    except FileNotFoundError:\n        raw = b""\n    sep', '    except FileNotFoundError:\n        raw = b"x"\n    sep')]),
    ("r6_f9_row_ends_in_lf", SS, [('json.dumps(row, sort_keys=True).encode("utf-8") + b"\\n")', 'json.dumps(row, sort_keys=True).encode("utf-8"))')]),
    ("r6_f9_go_file_check", SS, [("    check_go_append(path, load_go(path), row)", "    return None")]),
    ("r6_f9_history_reads_file", SS, [("    rows = load_history(path)\n    why = HISTORY.problem(row)", "    rows = []\n    why = HISTORY.problem(row)")]),
    ("r6_f9_history_row_checked", SS, [("    why = HISTORY.problem(row)\n    if why:\n        raise RowRefused", "    why = HISTORY.problem(row)\n    if False:\n        raise RowRefused")]),
    ("r6_f9_plan_uses_check", SS, [("        append_row(path, row, check_plan_append)", "        append_row(path, row, lambda p, r: None)")]),
    ("r6_f9_pending_failed_checked", SS, [('            why, line = failed_problem(row, reservation), "failed"', '            why, line = None, "failed"')]),
    ("r6_f9_pending_raise", SS, [("        if why:\n            raise RowRefused(label(path), line, why)", "        if False:\n            raise RowRefused(label(path), line, why)")]),
    # -- fresh WOULD ids from go_ledger.py next-id
    ("r7_nid_max_plus_one", GL, [("max(ks, default=0) + 1", "len(ks) + 1")]),
    ("r7_nid_starts_at_one", GL, [("max(ks, default=0) + 1", "max(ks, default=1) + 1")]),
    ("r7_nid_counts_grants", GL, [('used = {r["id"] for r in SS.load_go(ledger())}', 'used = {r["id"] for r in SS.load_go(ledger()) if r["revoked"]}')]),
    ("r7_nid_reads_ledger", GL, [('used = {r["id"] for r in SS.load_go(ledger())}', "used = set()")]),
    ("r7_nid_prefix_has_head", GL, [('prefix = f"{target}-{action}-{head[:8]}-"', 'prefix = f"{target}-{action}-aaaaaaaa-"')]),
    ("r7_nid_prefix_has_target", GL, [('prefix = f"{target}-{action}-{head[:8]}-"', 'prefix = f"101-{action}-{head[:8]}-"')]),
    ("r7_nid_k_format", GL, [('re.fullmatch(r"[1-9][0-9]{0,8}", u[len(prefix):])', 're.fullmatch(r"[0-9]+", u[len(prefix):])')]),
    ("r7_nid_pending_counted", GL, [('            used.update(_id_strings(SS.loads(raw.decode("utf-8"))))', '            SS.loads(raw.decode("utf-8"))')]),
    ("r7_nid_pending_keys", GL, [("            yield k\n            yield from _id_strings(v)", "            yield from _id_strings(v)")]),
    ("r7_nid_pending_values", GL, [("            yield k\n            yield from _id_strings(v)", "            yield k")]),
    ("r7_nid_pending_lists", GL, [("    elif isinstance(value, list):\n        for v in value:\n            yield from _id_strings(v)", "    elif False:\n        pass")]),
    ("r7_nid_pending_default_optional", GL, [('        if pending is not None:\n            raise ValueError("pending file not found")', '        if True:\n            raise ValueError("pending file not found")')]),
    ("r7_nid_pending_explicit_required", GL, [('        if pending is not None:\n            raise ValueError("pending file not found")', '        if False:\n            raise ValueError("pending file not found")')]),
    ("r7_nid_pending_bad_json", GL, [('            raise ValueError("pending file is not JSON") from None', "            pass")]),
    ("r7_nid_bad_pr", GL, [("        if (a.pr is not None and not SS.PR_RE.fullmatch(a.pr)) \\", "        if False \\")]),
    ("r7_nid_bad_branch", GL, [("                or (a.branch is not None and not BRANCH_RE.fullmatch(a.branch)) \\", "                or False \\")]),
    ("r7_nid_bad_action", GL, [("                or not SS.ACTION_RE.fullmatch(a.action) or not SHA_RE.fullmatch(a.head):", "                or not SHA_RE.fullmatch(a.head):")]),
    ("r7_nid_bad_head", GL, [("                or not SS.ACTION_RE.fullmatch(a.action) or not SHA_RE.fullmatch(a.head):", "                or not SS.ACTION_RE.fullmatch(a.action):")]),
    ("r7_nid_branch_slash", GL, [('a.branch.replace("/", ".")', "a.branch")]),
    ("r7_nid_wid_checked", GL, [("        if not WID_RE.fullmatch(wid):\n", "        if False:\n")]),
    ("r7_nid_corrupt_fails_closed", GL, [("        except (SS.StateCorrupt, ValueError) as exc:\n            print(f\"ERROR {exc}\")\n            return 2\n        if not WID_RE",
                                          "        except KeyError as exc:\n            print(f\"ERROR {exc}\")\n            return 2\n        if not WID_RE")]),  # StateCorrupt is a ValueError
    # -- docs: each put back to its old WOULD-id recipe
    ("r7_doc_spb_would_id", SPB, [("   `<wid>` = the output of `python3 KIT/bin/go_ledger.py next-id --pr <N> --action <action> --head <full sha>`:", "   `<wid>` = `<N>-<action>-<head8>`.")]),
    ("r7_doc_spb_clear_id", SPB, [("(`<wid>` = the output of `python3 KIT/bin/go_ledger.py next-id --pr <N> --action clear_hold --head <the PR head>`, a fresh id per request)", "(`<wid>` = `<N>-clear_hold-<id>`)")]),
    ("r7_doc_spb_renewal", SPB, [("   with a new id from `go_ledger.py next-id`, never the old one.", "")]),
    ("r7_doc_conductor_oneoff", CPB, [("Take a fresh `<wid>` from `python3 KIT/bin/go_ledger.py next-id --pr <N> --action <a> --head <sha>` (`--branch <name>` when there is no PR). ", "")]),
    ("r7_doc_shepherd_profile", PSH, [("; `<wid>` is always a fresh `go_ledger.py next-id`)", ")")]),
    ("r7_doc_conductor_profile", CCO, [("with a fresh id `<N>-<action>-<head8>-<k>` from\n  `go_ledger.py next-id`", "with an id")]),
    ("r7_doc_publisher", PUB, [("takes a fresh `<wid>` (`<N>-<action>-<head8>-<k>`) from the read-only `bin/go_ledger.py next-id --pr <N> --action <a> --head <expected remote head>` (`--branch <B>` with no PR), and ", "")]),
    ("r7_doc_armer", ARM_T, [("(`<wid>` = the WOULD's fresh `<N>-<action>-<head8>-<k>` id from `go_ledger.py next-id`; ", "(")]),
    # the call sites, each put back to its pre-round-6 append
    ("r6_f9_go_call_site", GL, [("        SS.append_row(ledger(), row, SS.check_go_file_append)\n",
                                 '        SS.check_go_file_append(ledger(), row)\n        with open(ledger(), "a") as fh:\n            fh.write(__import__("json").dumps(row, sort_keys=True) + "\\n")\n')]),
    ("r6_f9_sent_call_site", OB, [("    SS.append_row(outbox_path(), rec, SS.check_history_append)\n",
                                   '    with open(outbox_path(), "a", encoding="utf-8") as fh:\n        fh.write(json.dumps(rec) + "\\n")\n')]),
    ("r6_f9_proof_call_site", OB, [('    SS.append_row(entry(key, "pending"), proof, SS.pending_append_check(key))\n',
                                    '    SS.pending_append_check(key)(entry(key, "pending"), proof)\n    with open(entry(key, "pending"), "a", encoding="utf-8") as fh:\n        fh.write("\\n" + json.dumps(proof))\n')]),
    ("r6_f9_failed_call_site", OB, [("    SS.append_row(pending, rec, SS.pending_append_check(key, failed=True))\n",
                                     '    with open(pending, "a", encoding="utf-8") as fh:\n        fh.write("\\n" + json.dumps(rec) + "\\n")\n')]),
    ("r6_f9_plan_call_site", SS, [("        append_row(path, row, check_plan_append)\n",
                                   '        check_plan_append(path, row)\n        with open(path, "a", encoding="utf-8") as fh:\n            fh.write(json.dumps(row, sort_keys=True) + "\\n")\n')]),
    # -- the outbox
    ("ss_f6_history_rows_raise", SS, [('        why = HISTORY.problem(row)\n        if why:\n            raise StateCorrupt',
                                       '        why = HISTORY.problem(row)\n        if False:\n            raise StateCorrupt')]),
    ("ss_sent_key_bound", SS, [('    why = HISTORY.problem(row)\n    if why is None and row["key"] != key:', '    why = HISTORY.problem(row)\n    if False:')]),
    ("ss_sent_raise", SS, [('    why = sent_problem(row, key)\n    if why:\n        raise StateCorrupt', '    why = sent_problem(row, key)\n    if False:\n        raise StateCorrupt')]),
    ("ss_reservation_key_bound", SS, [('    why = RESERVATION.problem(row)\n    if why is None and row["key"] != key:', '    why = RESERVATION.problem(row)\n    if False:')]),
    ("ss_pending_empty", SS, [('        raise StateCorrupt(label(path), 1, "reservation:missing")', "        return {}, []")]),
    ("ss_reservation_raise", SS, [('    why = reservation_problem(reservation, key)\n    if why:\n        raise StateCorrupt',
                                   '    why = reservation_problem(reservation, key)\n    if False:\n        raise StateCorrupt')]),
    ("ss_f7_proofs_raise", SS, [('        why = proof_problem(proof, reservation)\n        if why:\n            raise StateCorrupt',
                                 '        why = proof_problem(proof, reservation)\n        if False:\n            raise StateCorrupt')]),
    ("ss_f7_proof_fields", SS, [('    why = PROOF.problem(proof)\n    if why:\n        return why', '    why = PROOF.problem(proof)\n    if False:\n        return why')]),
    ("ss_f7_proof_owner", SS, [('    if proof["owner"] != reservation["owner_terminal"]:', "    if False:")]),
    ("ss_f7_proof_generation", SS, [('    if proof["generation"] != reservation["generation"]:', "    if False:")]),
    ("ss_f7_proof_evidence", SS, [('    if proof["evidence"] != gone_evidence(proof["owner"]):', "    if False:")]),
    ("ss_f7_proof_time", SS, [('    if proof["proven_at"] != iso_ns(proof["proven_ns"]):', "    if False:")]),
    ("ss_failed_by_evidence", SS, [('    if (row["by"] == "owner_token") != (row["evidence"] is None):', "    if False:")]),
    ("ss_failed_generation", SS, [('        if ev["generation"] != reservation["generation"]:', "        if False:")]),
    ("ss_failed_proof_bound", SS, [('        why = proof_problem(ev["proof"], reservation)\n        return why and f"evidence.proof.{why}"', "        return None")]),
    ("ss_generation_format", SS, [('    if not GENERATION_RE.fullmatch(body):', "    if False:")]),
    ("ss_generation_no_zero", SS, [('GENERATION_RE = re.compile(r"[1-9][0-9]{0,11}")', 'GENERATION_RE = re.compile(r"[0-9]{1,12}")')]),
    ("ss_generation_write", SS, [("    if type(gen) is not int or gen < 1:", "    if False:")]),
    # -- holds.json
    ("ss_holds_cleared_list", SS, [('            if not isinstance(v, list):\n                return "_cleared", "_cleared:type"', '            if False:\n                return "_cleared", "_cleared:type"')]),
    ("ss_holds_cleared_rows", SS, [('                why = CLEARED.problem(row)\n                if why:', "                why = CLEARED.problem(row)\n                if False:")]),
    ("ss_holds_hold_rows", SS, [('            why = HOLD.problem(v)\n            if why:', "            why = HOLD.problem(v)\n            if False:")]),
    ("ss_holds_unknown_key", SS, [('            return k, "row_type:unknown (want a PR number or _cleared)"', "            continue")]),
    ("ss_holds_raise", SS, [("    bad = holds_problem(doc)\n    if bad:\n        raise StateCorrupt", "    bad = holds_problem(doc)\n    if False:\n        raise StateCorrupt")]),
    ("ss_holds_write_checked", SS, [("    bad = holds_problem(doc)\n    if bad:\n        raise RowRefused", "    bad = holds_problem(doc)\n    if False:\n        raise RowRefused")]),
    ("ss_items_empty", SS, [('    if not v:\n        return "empty"\n    for i, item in enumerate(v):', '    if False:\n        return "empty"\n    for i, item in enumerate(v):')]),
    ("ss_items_checked", SS, [("        why = HELD_ITEM.problem(item)\n        if why:", "        why = HELD_ITEM.problem(item)\n        if False:")]),
    ("ss_cleared_item_numeric", SS, [('"item_id": pattern(ITEM_RE),', '"item_id": pattern(HELD_ITEM_RE),')]),
    # -- plan-approvals.jsonl
    ("ss_plan_baseline_checked", SS, [("        return BASELINE.problem(row)", "        return None")]),
    ("ss_plan_approval_checked", SS, [("        return APPROVAL.problem(row)", "        return None")]),
    ("ss_plan_unknown_kind", SS, [('    return "row_type:unknown (kind must be baseline or approval)"', "    return None")]),
    ("ss_plan_rows_raise", SS, [("        why = plan_row_problem(row)\n        if why:\n            raise StateCorrupt", "        why = plan_row_problem(row)\n        if False:\n            raise StateCorrupt")]),
    ("ss_plan_append_checked", SS, [("    why = plan_row_problem(row)\n    if why:\n        raise RowRefused", "    why = plan_row_problem(row)\n    if False:\n        raise RowRefused")]),
    ("ss_plan_append_reads_file", SS, [("    rows = load_plan_ledger(path)\n    why = plan_row_problem(row)", "    rows = []\n    why = plan_row_problem(row)")]),
    # -- heavy/active/owner.json
    ("ss_owner_raise", SS, [("    why = HEAVY_OWNER.problem(row)\n    if why:\n        raise StateCorrupt", "    why = HEAVY_OWNER.problem(row)\n    if False:\n        raise StateCorrupt")]),
    ("ss_owner_write_checked", SS, [("    why = HEAVY_OWNER.problem(row)\n    if why:\n        raise RowRefused", "    why = HEAVY_OWNER.problem(row)\n    if False:\n        raise RowRefused")]),
    # -- strict JSON and line reading
    ("ss_json_duplicates", SS, [("        if k in out:\n            raise ValueError", "        if False:\n            raise ValueError")]),
    ("ss_json_nan", SS, [("    return json.loads(text_, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)",
                          "    return json.loads(text_, object_pairs_hook=_no_duplicates)")]),
    ("ss_jsonl_blank_line", SS, [('            raise StateCorrupt(label(path), n, "row:blank_line")', "            continue")]),
    ("ss_jsonl_not_json", SS, [('            raise StateCorrupt(label(path), n, "row:not_json") from None', "            continue")]),
    ("ss_jsonl_not_utf8", SS, [('            raise StateCorrupt(label(path), n, "row:not_utf8") from None', "            continue")]),
    ("ss_jsonl_final_newline", SS, [('    if lines and lines[-1] == b"":\n', "    if False:\n")]),
    ("ss_doc_not_json", SS, [('        raise StateCorrupt(label(path), 1, "document:not_json") from None', "        return {}")]),
    ("ss_label_relative", SS, [("        return os.path.relpath(full, sd)", "        return os.path.basename(full)")]),
    # -- call sites: every reader and writer uses the validator
    ("cm_ss_ledger_validated", CM, [("    return SS.load_go(path)", "    return [r for _, r in SS.jsonl_rows(path)]")]),
    ("cm_ss_corrupt_denies", CM, [('    except SS.StateCorrupt as exc:  # state_corrupt:go.jsonl:<line>:<why>\n        word, detail = "DENY", str(exc)',
                                   '    except SS.StateCorrupt as exc:  # state_corrupt:go.jsonl:<line>:<why>\n        word, detail = "ALLOW", str(exc)')]),
    ("gl_ss_reads_ledger", SS, [("    check_go_append(path, load_go(path), row)", "    check_go_append(path, [], row)")]),
    ("gl_ss_row_checked", GL, [("SS.append_row(ledger(), row, SS.check_go_file_append)", "SS.append_row(ledger(), row, lambda p, r: None)")]),
    ("ob_ss_history_validated", OB, [('    for row in SS.load_history(outbox_path()):\n        if row["key"] == key:',
                                      '    for row in [r for _, r in SS.jsonl_rows(outbox_path())]:\n        if isinstance(row, dict) and row.get("key") == key:')]),
    ("ob_ss_sent_validated", OB, [('        return SS.load_sent(entry(key, "sent"), key)', '        return SS.json_doc(entry(key, "sent"))')]),
    ("ob_ss_pending_validated", OB, [('    return SS.load_pending(entry(key, "pending"), key)',
                                      '    rows = [r for _, r in SS.jsonl_rows(entry(key, "pending"))]\n    return rows[0], rows[1:]')]),
    ("ob_ss_generation_validated", OB, [('    return SS.load_generation(entry(key, "generation"))',
                                         '    p = entry(key, "generation")\n    return int(open(p).read().strip() or 0) if os.path.exists(p) else 0')]),
    ("ob_ss_check_corrupt_pending", OB, [('repairs it by hand)")\n                        return PENDING_EXIT', 'repairs it by hand)")\n                        return 0')]),
    ("ob_ss_sent_corrupt_pending", OB, [('nothing changed (the reservation stays PENDING)")\n                    return 1', 'nothing changed (the reservation stays PENDING)")\n                    gen = None')]),
    ("ob_ss_owner_gone_corrupt", OB, [("                    proof, why = None, str(exc)", "                    raise")]),
    ("ob_ss_failed_corrupt", OB, [("                how, evidence = None, str(exc)", "                raise")]),
    ("ob_ss_reservation_checked", OB, [("    why = SS.reservation_problem(rec, key)\n    if why:", "    why = SS.reservation_problem(rec, key)\n    if False:")]),
    ("ob_ss_proof_checked", SS, [("            why, line = proof_problem(row, reservation), len(proofs) + 2", "            why, line = None, len(proofs) + 2")]),
    ("ob_ss_sent_checked", OB, [("    why = SS.sent_problem(rec, key)\n    if why:", "    why = SS.sent_problem(rec, key)\n    if False:")]),
    ("ob_ss_failed_checked", OB, [("    why = SS.failed_problem(rec, reservation)\n    if why:", "    why = SS.failed_problem(rec, reservation)\n    if False:")]),
    ("ob_ss_generation_checked", OB, [('    SS.check_generation_write(entry(key, "generation"), gen)\n', "")]),
    ("ag_ss_holds_validated", AG, [("    return SS.load_holds(path)", "    return SS.json_doc(path) if os.path.exists(path) else {}")]),
    ("ag_ss_hold_write_checked", AG, [("        SS.check_holds_write(path, holds)  # never write a hold the readers would reject\n", "")]),
    ("ag_ss_corrupt_denies", AG, [('        word, detail = "DENY", str(exc)\n    except BadInput', '        word, detail = "ARM", str(exc)\n    except BadInput')]),
    ("hd_ss_ledger_corrupt_refused", HD, [("    except SS.StateCorrupt as exc:  # state_corrupt:go.jsonl:<line>:<why>\n        return refuse(str(exc))",
                                           "    except SS.StateCorrupt as exc:  # state_corrupt:go.jsonl:<line>:<why>\n        records = []")]),
    ("hd_ss_holds_read_validated", HD, [("        holds = GATE.read_holds(path)\n", "        holds = SS.json_doc(path) if os.path.exists(path) else {}\n")]),
    ("hd_ss_cleared_checked", HD, [("        SS.check_holds_write(path, holds)\n", "")]),
    ("rg_ss_owner_validated", RG, [('        return SS.load_owner(os.path.join(marker, "owner.json"))', '        return SS.json_doc(os.path.join(marker, "owner.json"))')]),
    ("rg_ss_owner_write_checked", RG, [('    SS.check_owner_write(os.path.join(marker, "owner.json"), data)  # never write what _owner rejects\n', "")]),
    ("rg_ss_corrupt_refused", RG, [('    except SS.StateCorrupt as exc:\n        print(f"REFUSED {exc} (report it to the conductor; never take it over)")\n        return 1',
                                    '    except SS.StateCorrupt as exc:\n        print(f"REFUSED {exc} (report it to the conductor; never take it over)")\n        return 0')]),
    ("ph_ss_ledger_validated", PH, [("    for row in SS.load_plan_ledger(path):", "    for row in [r for _, r in SS.jsonl_rows(path)]:")]),
    ("ph_ss_corrupt_refused", PH, [('        print(f"REFUSE corrupt_ledger {exc}")\n        return 1', '        print(f"REFUSE corrupt_ledger {exc}")\n        return 0')]),
]
# the state_schema tests (test_state_*.py) exercise each script's reads and writes of its
# state files, so every script that reads or writes one also runs its test_state_* file.
ST_ALL = ["test_state_schema.py", "test_state_go.py", "test_state_holds.py", "test_state_plan_owner.py",
          "test_state_outbox.py", "test_state_append.py"]
TEST_FOR = {CM: ["test_check_mandate.py", "test_publisher_push.py", "test_holds.py", "test_state_go.py"],
            GL: ["test_check_mandate.py", "test_state_go.py", "test_state_append.py", "test_would_ids.py"],
            AG: ["test_automerge_gate.py", "test_state_holds.py"], AE: ["test_automerge_gate.py"],
            OB: ["test_outbox.py", "test_state_outbox.py", "test_state_append.py"],
            RG: ["test_resource_gate.py", "test_state_plan_owner.py"],
            PH: ["test_plan_hash_check.py", "test_state_plan_owner.py"], RO: ["test_remap_owners.py"],
            HD: ["test_holds.py", "test_automerge_gate.py", "test_state_holds.py", "test_state_go.py"],
            SS: ST_ALL, MJ: ["test_docs.py"], RM: ["test_docs.py"],
            PUB: ["test_docs.py", "test_publisher_push.py"], SPB: ["test_docs.py"],
            CPB: ["test_docs.py"], PSH: ["test_docs.py"], CCO: ["test_docs.py"], ARM_T: ["test_docs.py"]}
DOC_FILES = ("README.md", "conductor-playbook.md", "shepherd-playbook.md")
DOC_DIRS = ("agents", "templates")


def make_copy(dest):
    if dest.exists():
        raise SystemExit(f"refusing to reuse existing {dest}")
    dest.mkdir(parents=True)
    shutil.copytree(KIT / "bin", dest / "bin", ignore=shutil.ignore_patterns("__pycache__"))
    for d in DOC_DIRS:  # the doc-contract tests and the push test read these from the copy
        shutil.copytree(KIT / d, dest / d)
    for f in ("mandates.json", "resources.json") + DOC_FILES:
        shutil.copy2(KIT / f, dest / f)
    (dest / "state").mkdir()
    ledger = KIT / "state" / "plan-approvals.jsonl"  # your trusted baselines, if you recorded any
    if ledger.exists():
        shutil.copy2(ledger, dest / "state" / "plan-approvals.jsonl")


def run_tests(kit_copy, test_files, basetemp):
    basetemp.parent.mkdir(parents=True, exist_ok=True)
    # HOME stays real: pytest may live in the user site-packages. Scripts under test still get
    # a temp HOME from conftest.base_env.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
           "PYTHONDONTWRITEBYTECODE": "1", "SUPER_AGENT_KIT": str(kit_copy)}
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-x",
                        f"--basetemp={basetemp}", *(str(TESTS / t) for t in test_files)],
                       cwd=str(basetemp.parent), env=env, capture_output=True, text=True, timeout=600)
    return r.returncode, r.stdout + r.stderr


def main():
    scratch = Path(sys.argv[1]).resolve()
    exact = {a[1:] for a in sys.argv[2:] if a.startswith("=")}  # =LABEL picks exactly that mutant
    prefixes = tuple(a for a in sys.argv[2:] if not a.startswith("="))
    chosen = [mt for mt in M if not (prefixes or exact) or mt[0] in exact or (prefixes and mt[0].startswith(prefixes))]
    unknown = exact - {mt[0] for mt in M}
    if unknown:
        print(f"unknown labels: {sorted(unknown)}")
        return 2
    scratch.mkdir(parents=True, exist_ok=True)
    control = scratch / "control"
    make_copy(control)
    files = sorted({t for mt in chosen for t in TEST_FOR[mt[1]]})  # fresh control for this chunk
    for tf in files:
        rc, out = run_tests(control, [tf], scratch / "bt-control" / tf.replace(".py", ""))
        if rc != 0:
            print(f"CONTROL FAILED {tf} rc={rc}\n{out[-2000:]}")
            return 2
    print(f"control: all {len(files)} test files pass on the unmutated copy")
    results = {"KILLED": 0, "SURVIVED": 0, "SETUP_ERROR": 0}
    for label, script, edits in chosen:
        d = scratch / f"mut-{label}"
        make_copy(d)
        path = d / "bin" / script
        src = path.read_text()
        setup_err = None
        for old, new in edits:
            if src.count(old) != 1:
                setup_err = f"pattern matched {src.count(old)}x: {old!r}"
                break
            src = src.replace(old, new)
        if setup_err is None:
            path.write_text(src)
            rc, out = run_tests(d, TEST_FOR[script], scratch / f"bt-{label}")
            verdict = {1: "KILLED", 0: "SURVIVED"}.get(rc, "SETUP_ERROR")
            if verdict == "KILLED":
                failed = [ln for ln in out.splitlines() if ln.startswith("FAILED")]
                note = failed[0].split("::", 1)[-1][:90] if failed else "?"
            else:
                note = f"rc={rc}"
        else:
            verdict, note = "SETUP_ERROR", setup_err
        results[verdict] += 1
        print(f"{verdict:<11} {label:<32} {note}")
    print("TOTAL " + " ".join(f"{k}={v}" for k, v in results.items()) + f" of {len(chosen)}")
    return 0 if results["SURVIVED"] == 0 and results["SETUP_ERROR"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
