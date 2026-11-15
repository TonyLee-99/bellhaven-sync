"""Regression tests: one per edge case or defect found while reviewing the design and the code."""
import random
import re

import httpx
import pytest

from bellhaven_sync.executor import StaleProposal, apply_proposal

BH = "0015QAPLGS3FVYEEEM"
HARBORVIEW = "001FJZYHR7MLFMNPLL"


def _edit_page(env, rel, old, new):
    p = env.site_page(rel)
    html = p.read_text()
    assert old in html, (rel, old)
    p.write_text(html.replace(old, new))


def test_any_approval_order_gives_the_same_end_state(env):
    env.run()
    keys = [p["key"] for p in env.pending()]
    random.Random(7).shuffle(keys)
    for k in keys:
        env.approve(k)
    reference = {k: {f: v for f, v in a.items() if f not in ("updated_at", "note", "account_id")}
                 for k, a in env.store.accounts.items() if not a["created_by_candidate"]}
    assert reference["001H1JMVZWP46D5VUF"]["name"] == "Bellhaven of Zanesville"
    assert reference["001H1JMVZWP46D5VUF"]["parent_id"] == BH
    assert env.run()["queue"]["new"] == 0


# ---------------------------------------------------------------- never two accounts from one create
def test_post_timeout_after_commit_creates_exactly_one_account(env, monkeypatch):
    env.run()
    p = env.pending(kind="CREATE", title="Batavia")[0]
    real = env.crm.http.request
    state = {"done": False}

    def timeout_after_commit(method, url, **kw):
        r = real(method, url, **kw)
        if method == "POST" and not state["done"]:
            state["done"] = True
            raise httpx.ReadTimeout("response lost", request=httpx.Request(method, url))
        return r

    monkeypatch.setattr(env.crm.http, "request", timeout_after_commit)
    env.approve(p["key"])                                   # recognised by its marker, no second POST
    batavias = [a for a in env.store.accounts.values() if a["name"] == "Bellhaven of Batavia"]
    assert len(batavias) == 1 and env.state.get(p["key"])["status"] == "applied"
    assert sum(1 for x in env.store.log if x["op"] == "POST") == 1


def test_wrapped_post_response_is_understood(env, monkeypatch):
    env.run()
    p = env.pending(kind="CREATE", title="Carlisle")[0]
    real = env.crm._req

    def wrapped(method, path, **kw):
        out = real(method, path, **kw)
        return {"data": out} if method == "POST" else out

    monkeypatch.setattr(env.crm, "_req", wrapped)
    env.approve(p["key"])
    assert len([a for a in env.store.accounts.values() if a["name"] == "Bellhaven of Carlisle"]) == 1


def test_crash_between_post_and_log_does_not_duplicate_on_retry(env, monkeypatch):
    env.run()
    p = env.pending(kind="CREATE", title="Amberly")[0]
    real_create = env.crm.create_account

    def create_then_crash(fields):
        real_create(fields)
        raise RuntimeError("process died after POST")

    monkeypatch.setattr(env.crm, "create_account", create_then_crash)
    with pytest.raises(RuntimeError):
        env.approve(p["key"])
    monkeypatch.setattr(env.crm, "create_account", real_create)
    assert env.state.claim_retry(p["key"], "t")
    apply_proposal(env.crm, env.state, p["key"])
    amberly_oh = [a for a in env.store.accounts.values() if a["name"] == "Amberly Manor" and a["billing_state"] == "OH"]
    assert len(amberly_oh) == 1


# ---------------------------------------------------------------- verification and value representation
def test_failed_verification_is_not_turned_into_applied_by_retry(env, monkeypatch):
    env.run()
    p = env.pending(kind="ADDRESS", title="Portsmouth")[0]
    real = env.crm.patch_account
    calls = {"n": 0}

    def ignores_first(aid, fields):
        calls["n"] += 1
        if calls["n"] == 1:
            return env.crm.get_account(aid)                 # server silently ignored the write
        return real(aid, fields)

    monkeypatch.setattr(env.crm, "patch_account", ignores_first)
    with pytest.raises(Exception):
        env.approve(p["key"])
    assert env.state.get(p["key"])["status"] == "failed"
    assert env.state.claim_retry(p["key"], "t")
    apply_proposal(env.crm, env.state, p["key"])            # writes again, verifies for real
    assert env.store.accounts["001CF3LDWVRGL09P4F"]["billing_zip"] == "45662"


def test_server_side_normalisation_and_nulls_are_not_drift(env, monkeypatch):
    env.run()
    real = env.crm.get_account

    def as_other_api(aid):
        a = dict(real(aid))
        for k in ("parent_id", "duplicate_of_account", "chow_current_account", "note"):
            a[k] = a[k] or None                              # null instead of ""
        a["lifetime_revenue"] = f"{a['lifetime_revenue']:.2f}"  # money as a string
        if a.get("billing_zip") and len(a["billing_zip"]) == 5:
            a["billing_zip"] += "-0000"                      # ZIP+4
        return a

    monkeypatch.setattr(env.crm, "get_account", as_other_api)
    env.approve_all()
    assert not env.pending()


# ---------------------------------------------------------------- billing-locked old copy + existing Bellhaven copy
def test_locked_old_copy_is_chow_linked_to_existing_bellhaven_record(env):
    hv = env.store.accounts["001JD2MWRA74LTSN24"]                # Harborview copy of Port Clinton
    hv.update(lifetime_revenue=40000, outstanding_ar=2500)
    env.run()
    props = env.pending(slug="bellhaven-of-port-clinton")
    assert [p["kind"] for p in props] == ["CHOW"] and not env.pending(kind="CREATE", title="Port Clinton")
    env.approve(props[0]["key"])
    assert hv["chow_current_account"] == "001UELXDAKFRKB8932" and hv["parent_id"] == HARBORVIEW and hv["status"] == "Active"
    assert env.store.accounts["001UELXDAKFRKB8932"]["status"] == "Active"


# ---------------------------------------------------------------- status on its own
def test_inactive_listed_location_is_reactivated(env):
    env.store.accounts["001JQER1RMYKNRLNME"]["status"] = "Inactive"     # Goshen, everything else correct
    env.run()
    assert [p["kind"] for p in env.pending(slug="bellhaven-of-goshen")] == ["REACTIVATE"]


def test_flagged_location_that_comes_back_is_reactivated(env):
    env.store.accounts["001W5G121A7SMRP58M"]["status"] = "Needs Review"  # Wooster flagged earlier
    env.run()
    assert [p["kind"] for p in env.pending(slug="bellhaven-of-wooster")] == ["REACTIVATE"]


def test_active_copy_beats_lower_id_inactive_copy(env):
    twin = dict(env.store.accounts["001JQER1RMYKNRLNME"], account_id="0010000000000000AA", status="Inactive")
    env.store.accounts[twin["account_id"]] = twin
    env.run()
    dup = env.pending(kind="DUPLICATE", slug="bellhaven-of-goshen")
    assert len(dup) == 1 and dup[0]["ops"][0]["account_id"] == "0010000000000000AA"


# ---------------------------------------------------------------- decisions are per logical change and situation
def test_rejected_reparent_stays_rejected_when_another_field_changes(env):
    env.run()
    rp = env.pending(kind="REPARENT", title="Zanesville")[0]
    assert env.state.reject(rp["key"], "t", "keep under Cedar Trail for now", rp["version"])
    env.store.accounts["001H1JMVZWP46D5VUF"]["billing_zip"] = "43702"   # unrelated drift
    env.run()
    assert not env.pending(kind="REPARENT", title="Zanesville")
    assert env.pending(kind="ADDRESS", slug="bellhaven-of-zanesville")


def test_website_name_reverted_after_rename_is_proposed_again(env):
    env.run()
    env.approve(env.pending(kind="RENAME", title="Chesterton")[0]["key"])
    page = "communities/bellhaven-of-chesterton.html"
    _edit_page(env, page, "<h1>Bellhaven of Chesterton</h1>", "<h1>Bellhaven Commons of Chesterton</h1>")
    env.run()
    env.approve(env.pending(kind="RENAME", slug="bellhaven-of-chesterton")[0]["key"])
    _edit_page(env, page, "<h1>Bellhaven Commons of Chesterton</h1>", "<h1>Bellhaven of Chesterton</h1>")
    env.run()
    again = env.pending(kind="RENAME", slug="bellhaven-of-chesterton")
    assert len(again) == 1 and again[0]["ops"][0]["fields"]["name"] == "Bellhaven of Chesterton"


def test_crm_reverted_after_apply_is_requeued_as_regressed(env):
    env.run()
    env.approve(env.pending(kind="ADDRESS", title="Portsmouth")[0]["key"])
    env.store.accounts["001CF3LDWVRGL09P4F"]["billing_zip"] = "45626"   # someone restored the old value
    stats = env.run()
    assert stats["queue"]["regressed"] == 1 and env.pending(kind="ADDRESS", title="Portsmouth")[0]["regressed"]


# ---------------------------------------------------------------- scraper / crawl completeness
def test_page_without_zip_is_quarantined_not_created(env):
    _edit_page(env, "communities/bellhaven-of-goshen.html", "Goshen, IN 46526", "Goshen, IN")
    stats = env.run()
    assert {q["slug"] for q in stats["quarantined"]} == {"bellhaven-of-goshen"}
    assert not env.pending(slug="bellhaven-of-goshen")


def test_homepage_failure_makes_the_crawl_incomplete(env):
    env.site_page("index.html").unlink()
    stats = env.run()
    assert not stats["crawl"]["complete"]["ok"] and "NOT_ON_SITE" not in stats["proposals_generated"]
    assert not env.pending(kind="NOT_ON_SITE", title="Findlay")


def test_four_page_directory_gives_the_same_proposals(env):
    env.run()
    before = {p["key"] for p in env.pending()}
    p3 = env.site_page("communities_page3.html").read_text()
    cards = re.findall(r'<div class="card">.*?</div>\s*</div>', p3, flags=re.S)
    assert len(cards) == 2
    env.site_page("communities_page4.html").write_text(p3.replace(cards[0], "").replace("Page 3 of 3", "Page 4 of 4"))
    page3 = p3.replace(cards[1], "").replace("Page 3 of 3", "Page 3 of 4").replace(
        "</p>\n</div>", '</p><a href="/communities?page=4">Next &rarr;</a>\n</div>', 1)
    page3 = page3.replace('<div class="wrap">', '<div class="wrap"><a href="/communities?page=4">Next &rarr;</a>', 1)
    env.site_page("communities_page3.html").write_text(page3)
    stats = env.run()
    assert stats["crawl"]["directory_pages"] == 4 and stats["crawl"]["locations"] == 35
    assert {p["key"] for p in env.pending()} == before


# ---------------------------------------------------------------- matching
def test_different_street_type_is_a_different_street(env):
    acc = env.store.accounts["001W5G121A7SMRP58M"]                     # website: 2301 Cleveland Road, Wooster
    acc.update(billing_street="2301 Cleveland Dr", name="Lakeside Care Center", phone="(330) 555-0100")
    env.run()
    assert env.pending(kind="CREATE", title="Wooster")                 # unrelated facility on another street


def test_street_type_typo_on_the_same_facility_is_an_address_fix(env):
    env.store.accounts["001W5G121A7SMRP58M"]["billing_street"] = "2301 Cleveland Dr"   # same name, city, zip, phone
    env.run()
    fix = env.pending(kind="ADDRESS", slug="bellhaven-of-wooster")
    assert fix and fix[0]["confidence"] == "medium" and not env.pending(kind="CREATE", title="Wooster")


def test_blank_street_matches_by_name_city_zip_instead_of_creating(env):
    env.store.accounts["001R4PXTUD9R6FNN8V"]["billing_street"] = ""                     # Defiance (has revenue + AR)
    env.run()
    assert not env.pending(kind="CREATE", title="Defiance")
    fix = env.pending(kind="ADDRESS", slug="bellhaven-of-defiance")
    assert fix and fix[0]["confidence"] == "medium"


def test_po_box_without_corroboration_does_not_match(env):
    acc = env.store.accounts["001NXP9X46CWEPSLSV"]                                       # Ashtabula PO Box
    acc.update(billing_zip="99999", phone="(000) 000-0000")
    env.run()
    assert env.pending(kind="CREATE", title="Ashtabula")


def test_two_website_locations_on_one_record_is_a_conflict(env):
    html = env.site_page("communities/bellhaven-of-defiance.html").read_text()
    env.site_page("communities/bellhaven-annex-of-defiance.html").write_text(
        html.replace("<h1>Bellhaven of Defiance</h1>", "<h1>Bellhaven Annex of Defiance</h1>"))
    _edit_page(env, "index.html", '<a href="/communities/bellhaven-meadows-of-findlay">',
               '<a href="/communities/bellhaven-annex-of-defiance">Annex</a><a href="/communities/bellhaven-meadows-of-findlay">')
    env.run()
    conflicts = env.pending(kind="CONFLICT")
    assert len(conflicts) == 1 and conflicts[0]["ops"][0]["account_id"] == "001R4PXTUD9R6FNN8V"
    assert not env.pending(kind="RENAME", title="Defiance")


def test_self_link_is_not_treated_as_resolved(env):
    acc = env.store.accounts["001W5G121A7SMRP58M"]
    acc["chow_current_account"] = acc["account_id"]
    env.run()
    assert not env.pending(kind="CREATE", title="Wooster")


# ---------------------------------------------------------------- pagination, state scoping, SOP switch
def test_shifted_pagination_is_reread_not_planned_from(env, monkeypatch):
    real = env.crm._req
    calls = {"n": 0}

    def shifting(method, path, **kw):
        out = real(method, path, **kw)
        if path == "/accounts" and method == "GET" and kw.get("params", {}).get("page") == 2 and calls["n"] == 0:
            calls["n"] += 1
            out = dict(out, data=[out["data"][0]] + out["data"])     # one row repeats, as if the list shifted
        return out

    monkeypatch.setattr(env.crm, "_req", shifting)
    accounts = env.crm.list_accounts()
    assert len(accounts) == len({a["account_id"] for a in accounts}) == 121


def test_state_is_scoped_per_crm_workspace(env, monkeypatch):
    a = env.settings.db_path
    monkeypatch.setenv("CRM_TOKEN", "another_candidate")
    assert env.settings.db_path != a


def test_create_mode_for_existing_successor(env):
    """Alternative config: literal SOP - new Millstone account, old one linked to it, planted copy retired."""
    env.settings.raw["sop"]["existing_successor"] = "create"
    env.run()
    p = env.pending(kind="CHOW", title="Sandusky")[0]
    env.approve(p["key"])
    old = env.store.accounts["001SXSF4ELF0Z2LGDM"]
    assert old["parent_id"] == BH and old["status"] == "Active"
    new = env.store.accounts[old["chow_current_account"]]
    assert new["created_by_candidate"] and new["parent_id"] == "001YRHHXQ5HJ0TCL2U" and new["status"] == "Active"
    assert (new["billing_street"], new["billing_zip"]) == ("2715 Columbus Ave", "44870")
    planted = env.store.accounts["0017JP8Z1UQ763BVK3"]
    assert planted["duplicate_of_account"] == new["account_id"] and planted["status"] == "Inactive"


# ---------------------------------------------------------------- crawl gaps, conflicts and broken links
def test_quarantined_page_does_not_get_its_account_flagged_missing(env):
    _edit_page(env, "communities/bellhaven-of-goshen.html", "Goshen, IN 46526", "Goshen, IN")
    env.run()
    assert not env.pending(title="Goshen")
    assert env.store.accounts["001JQER1RMYKNRLNME"]["status"] == "Active"


def test_approved_conflict_does_not_come_back_every_day(env):
    test_two_website_locations_on_one_record_is_a_conflict(env)
    env.approve(env.pending(kind="CONFLICT")[0]["key"])
    for _ in range(3):
        stats = env.run()
        assert stats["queue"]["regressed"] == 0 and not env.pending(kind="CONFLICT")


def test_chow_link_to_dead_target_is_not_resolved(env):
    old = env.store.accounts["001U6RW32TY0WSXZZB"]                                  # Tiffin, Cedar Trail, rev+AR
    dead = dict(env.store.accounts["001UXEM0PCSUVRAYPF"], account_id="001DEAD000000000XX", status="Inactive")
    env.store.accounts[dead["account_id"]] = dead
    old["chow_current_account"] = dead["account_id"]
    env.run()
    chow = env.pending(kind="CHOW", slug="bellhaven-of-tiffin")
    assert len(chow) == 1 and chow[0]["ops"][1]["old"]["chow_current_account"] == "001DEAD000000000XX"
    env.approve(chow[0]["key"])
    new = env.store.accounts[old["chow_current_account"]]
    assert new["name"] == "Bellhaven of Tiffin" and new["status"] == "Active" and new["parent_id"] == BH


def test_broken_link_on_the_live_record_is_surfaced(env):
    env.store.accounts["001JQER1RMYKNRLNME"]["duplicate_of_account"] = "001DOESNOTEXIST000"
    env.run()
    fix = env.pending(slug="bellhaven-of-goshen", title="broken link")
    assert len(fix) == 1 and fix[0]["ops"][0]["fields"] == {"duplicate_of_account": ""}


def test_rename_warns_when_the_record_is_not_yet_under_bellhaven(env):
    env.run()
    r = env.pending(kind="RENAME", title="Zanesville")[0]
    assert "separate proposal" in r["evidence"]["warning"]


def test_failed_detail_page_keeps_its_pending_proposals(env, monkeypatch):
    env.run()
    before = {p["key"] for p in env.pending(slug="bellhaven-of-zanesville")}
    real = env.scraper.parse_detail

    def down(slug, card):
        if slug == "bellhaven-of-zanesville":
            raise httpx.HTTPError("404")
        return real(slug, card)

    monkeypatch.setattr(env.scraper, "parse_detail", down)
    env.run()
    assert {p["key"] for p in env.pending(slug="bellhaven-of-zanesville")} == before


def test_review_app_requires_version_and_same_origin(env):
    from fastapi.testclient import TestClient
    from bellhaven_sync.review.app import create_app
    env.run()
    app = TestClient(create_app(env.settings, state=env.state, crm=env.crm))
    p = env.pending(kind="CREATE")[0]
    r = app.post(f"/p/{p['key']}/decide", data={"decision": "approve"}, follow_redirects=False)
    assert r.status_code == 303 and env.state.get(p["key"])["status"] == "pending"
    r = app.post(f"/p/{p['key']}/decide", data={"decision": "approve", "version": p["version"]},
                 headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and env.state.get(p["key"])["status"] == "pending"
    r = app.post(f"/p/{p['key']}/decide", data={"decision": "approve", "version": p["version"]}, follow_redirects=False)
    assert r.status_code == 303 and env.state.get(p["key"])["status"] == "applied"
    assert app.get(f"/p/{p['key']}").status_code == 200 and app.get("/").status_code == 200
    assert app.get("/history").status_code == 200


# ---------------------------------------------------------------- create recovery, guards and survivors
def _fail_first_patch(env, monkeypatch):
    real = env.crm.patch_account
    calls = {"n": 0}

    def flaky(aid, fields):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("network dropped")
        return real(aid, fields)
    monkeypatch.setattr(env.crm, "patch_account", flaky)


def test_retry_uses_logged_id_even_if_the_marker_was_edited_away(env, monkeypatch):
    env.run()
    p = env.pending(kind="CHOW", title="Tiffin")[0]
    _fail_first_patch(env, monkeypatch)
    with pytest.raises(RuntimeError):
        env.approve(p["key"])
    new = [a for a in env.store.accounts.values() if a["created_by_candidate"] and a["name"] == "Bellhaven of Tiffin"]
    assert len(new) == 1
    new[0]["note"] = "rep rewrote this note"                       # marker gone
    assert env.state.claim_retry(p["key"], "t")
    apply_proposal(env.crm, env.state, p["key"], env.settings.raw)
    tiffins = [a for a in env.store.accounts.values() if a["created_by_candidate"] and a["name"] == "Bellhaven of Tiffin"]
    assert len(tiffins) == 1 and env.store.accounts["001U6RW32TY0WSXZZB"]["chow_current_account"] == tiffins[0]["account_id"]


def test_marked_account_is_verified_and_corrected_before_linking(env, monkeypatch):
    env.run()
    p = env.pending(kind="CHOW", title="Tiffin")[0]
    _fail_first_patch(env, monkeypatch)
    with pytest.raises(RuntimeError):
        env.approve(p["key"])
    new = next(a for a in env.store.accounts.values() if a["created_by_candidate"] and a["name"] == "Bellhaven of Tiffin")
    new["parent_id"] = "001FWSQ30SFW6S7604"                        # someone put it under Cedar Trail
    assert env.state.claim_retry(p["key"], "t")
    apply_proposal(env.crm, env.state, p["key"], env.settings.raw)
    assert env.store.accounts[new["account_id"]]["parent_id"] == BH


def test_guard_allows_only_the_sibling_value_not_any_value(env):
    env.run()
    rp = env.pending(kind="REPARENT", title="Zanesville")[0]
    env.store.accounts["001H1JMVZWP46D5VUF"]["name"] = "Entirely Different Facility"
    with pytest.raises(StaleProposal):
        env.approve(rp["key"])
    env2_rename = env.pending(kind="RENAME", title="Zanesville")
    assert env2_rename                                               # the rename is untouched, still pending


def test_duplicate_retire_needs_a_valid_active_survivor(env):
    env.run()
    d = env.pending(kind="DUPLICATE", title="Port Clinton")[0]
    env.store.accounts["001UELXDAKFRKB8932"]["status"] = "Inactive"
    with pytest.raises(StaleProposal):
        env.approve(d["key"])
    assert env.store.accounts["001JD2MWRA74LTSN24"]["status"] == "Active"


def test_inactive_survivor_defers_its_copies(env):
    env.store.accounts["001UELXDAKFRKB8932"]["status"] = "Inactive"   # Bellhaven Port Clinton record
    stats = env.run()
    assert not env.pending(kind="DUPLICATE", slug="bellhaven-of-port-clinton")
    assert env.pending(kind="REACTIVATE", slug="bellhaven-of-port-clinton")
    assert any(d["location"] == "bellhaven-of-port-clinton" for d in stats["deferred"])


def test_resumed_consolidate_refuses_if_survivor_died(env, monkeypatch):
    env.run()
    c = env.pending(kind="CONSOLIDATE")[0]
    real = env.crm.patch_account
    calls = {"n": 0}

    def fail_second(aid, fields):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("dropped")
        return real(aid, fields)
    monkeypatch.setattr(env.crm, "patch_account", fail_second)
    with pytest.raises(RuntimeError):
        env.approve(c["key"])
    monkeypatch.setattr(env.crm, "patch_account", real)
    env.store.accounts["001WR41PYNWXCAE2X4"]["status"] = "Inactive"
    assert env.state.claim_retry(c["key"], "t")
    with pytest.raises(StaleProposal):
        apply_proposal(env.crm, env.state, c["key"], env.settings.raw)
    assert env.state.get(c["key"])["status"] == "failed"              # partial: stays locked for a person
    assert env.store.accounts["001B7XZAA3AFALS9GP"]["status"] == "Active"


def test_chow_create_refused_if_a_successor_was_created_by_hand(env):
    env.run()
    p = env.pending(kind="CHOW", title="Tiffin")[0]
    hand = dict(env.store.accounts["001U6RW32TY0WSXZZB"], account_id="001HANDMADE0000001", parent_id=BH,
                lifetime_revenue=0, outstanding_ar=0, note="")
    env.store.accounts[hand["account_id"]] = hand
    with pytest.raises(StaleProposal):
        env.approve(p["key"])
    assert not [a for a in env.store.accounts.values() if a["created_by_candidate"]]


def test_create_refused_if_a_corroborated_po_box_account_appeared(env):
    env.run()
    p = env.pending(kind="CREATE", title="Batavia")[0]
    env.store.accounts["001HANDMADE0000002"] = dict(env.store.accounts["001NXP9X46CWEPSLSV"], account_id="001HANDMADE0000002",
        name="Bellhaven of Batavia", billing_street="PO Box 12", billing_city="Batavia", billing_zip="45103", note="")
    with pytest.raises(StaleProposal):
        env.approve(p["key"])


def test_any_quarantined_page_switches_absence_checks_off(env):
    _edit_page(env, "communities/bellhaven-of-chagrin-falls.html", "150 River St<br>", "<br>")
    stats = env.run()
    assert stats["quarantined"] and not env.pending(kind="NOT_ON_SITE")
    assert not env.pending(title="Riverbend")


def test_rejected_street_fix_stays_rejected_when_the_zip_is_fixed(env):
    acc = env.store.accounts["001W5G121A7SMRP58M"]
    acc.update(billing_street="2301 Cleveland Rd Suite 1", billing_zip="44690")
    env.run()
    street = env.pending(kind="ADDRESS", title="Fix street of Bellhaven of Wooster")[0]
    assert env.state.reject(street["key"], "t", "suite number is correct", street["version"])
    acc["billing_zip"] = "44691"                                     # rep fixes only the zip
    env.run()
    assert not env.pending(kind="ADDRESS", title="Fix street of Bellhaven of Wooster")


def test_rejected_consolidate_stays_rejected_when_a_copy_is_resolved_by_hand(env):
    env.run()
    c = env.pending(kind="CONSOLIDATE")[0]
    assert env.state.reject(c["key"], "t", "not yet", c["version"])
    env.store.accounts["001B7XZAA3AFALS9GP"].update(duplicate_of_account="001WR41PYNWXCAE2X4", status="Inactive")
    env.run()
    assert not env.pending(kind="CONSOLIDATE")


def test_conflict_is_not_kept_alive_by_an_incomplete_crawl(env, monkeypatch):
    test_two_website_locations_on_one_record_is_a_conflict(env)
    real = env.scraper.parse_detail

    def broken(slug, card):
        if slug == "bellhaven-annex-of-defiance":
            return real("bellhaven-of-wooster", card).__class__(**{**real(slug, card).to_dict(), "street": "1 Elsewhere Rd"})
        if slug == "bellhaven-of-saline":
            raise ValueError("unrelated page broken")
        return real(slug, card)
    monkeypatch.setattr(env.scraper, "parse_detail", broken)
    env.run()
    assert not env.pending(kind="CONFLICT")


def test_three_cycle_of_duplicates_is_not_resolved(env):
    a = env.store.accounts
    w = a["001W5G121A7SMRP58M"]
    x = dict(w, account_id="001CYCLE0000000001", note="")
    y = dict(w, account_id="001CYCLE0000000002", note="")
    w["duplicate_of_account"], x["duplicate_of_account"], y["duplicate_of_account"] = x["account_id"], y["account_id"], w["account_id"]
    a[x["account_id"]], a[y["account_id"]] = x, y
    env.run()
    assert not env.pending(kind="CREATE", title="Wooster")


def test_active_duplicate_is_finished(env):
    env.store.accounts["001JD2MWRA74LTSN24"]["duplicate_of_account"] = "001UELXDAKFRKB8932"   # pointer set, still Active
    env.run()
    fin = env.pending(title="Finish retiring")
    assert len(fin) == 1 and fin[0]["ops"][0]["fields"] == {"status": "Inactive"}


def test_completely_different_street_is_not_repaired_by_fallback(env):
    env.store.accounts["001W5G121A7SMRP58M"].update(billing_street="999 Totally Different Avenue", phone="(330) 555-0199")
    env.run()
    assert not env.pending(kind="ADDRESS", slug="bellhaven-of-wooster")
    assert env.pending(kind="CREATE", title="Wooster")


def test_conflict_is_raised_even_if_the_record_is_already_needs_review(env):
    env.store.accounts["001R4PXTUD9R6FNN8V"]["status"] = "Needs Review"
    test_two_website_locations_on_one_record_is_a_conflict(env)


# ---------------------------------------------------------------- quarantine, survivors and the create-mode successor
def test_literal_sandusky_refused_if_the_existing_copy_was_retired_meanwhile(env):
    env.run()
    p = env.pending(kind="CHOW", title="Sandusky")[0]
    env.store.accounts["0017JP8Z1UQ763BVK3"]["status"] = "Inactive"
    with pytest.raises(StaleProposal):
        env.approve(p["key"])
    assert not [a for a in env.store.accounts.values() if a["created_by_candidate"]]


def test_quarantine_keeps_pending_absence_flags(env):
    env.run()
    flags = {p["key"] for p in env.pending(kind="NOT_ON_SITE")}
    _edit_page(env, "communities/bellhaven-of-goshen.html", "Goshen, IN 46526", "Goshen, IN")
    env.run()
    assert {p["key"] for p in env.pending(kind="NOT_ON_SITE")} == flags


def test_needs_review_survivor_is_not_flipped(env):
    env.run()
    env.store.accounts["001EGU7BMJ942ZTRE6"]["status"] = "Needs Review"               # Owosso survivor flagged by a person
    env.run()
    owosso = env.pending(slug="bellhaven-of-owosso")
    assert not any(p["kind"] == "DUPLICATE" and p["ops"][0]["account_id"] == "001EGU7BMJ942ZTRE6" for p in owosso)
    assert any(p["kind"] == "REACTIVATE" and p["ops"][0]["account_id"] == "001EGU7BMJ942ZTRE6" for p in owosso)


def test_literal_sandusky_successor_keeps_the_phone(env):
    env.run()
    env.approve(env.pending(kind="CHOW", title="Sandusky")[0]["key"])
    new = env.store.accounts[env.store.accounts["001SXSF4ELF0Z2LGDM"]["chow_current_account"]]
    assert new["phone"] == "(614) 307-3608"


# ---------------------------------------------------------------- recovery safety and unknown create outcomes
def _half_tiffin(env, monkeypatch):
    env.run()
    p = env.pending(kind="CHOW", title="Tiffin")[0]
    real = env.crm.patch_account
    state = {"n": 0}

    def flaky(aid, fields):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("dropped")
        return real(aid, fields)
    monkeypatch.setattr(env.crm, "patch_account", flaky)
    with pytest.raises(RuntimeError):
        env.approve(p["key"])
    monkeypatch.setattr(env.crm, "patch_account", real)
    new = next(a for a in env.store.accounts.values() if a["created_by_candidate"] and a["name"] == "Bellhaven of Tiffin")
    return p, new


def test_recovery_never_reparents_a_created_account_that_gained_billing(env, monkeypatch):
    p, new = _half_tiffin(env, monkeypatch)
    new.update(parent_id="001FWSQ30SFW6S7604", lifetime_revenue=100, outstanding_ar=50)
    n = len(env.store.log)
    assert env.state.claim_retry(p["key"], "t")
    with pytest.raises(StaleProposal):
        apply_proposal(env.crm, env.state, p["key"], env.settings.raw)
    assert new["parent_id"] == "001FWSQ30SFW6S7604" and len(env.store.log) == n      # nothing written


def test_recovered_account_that_became_a_duplicate_is_not_used(env, monkeypatch):
    p, new = _half_tiffin(env, monkeypatch)
    new.update(status="Inactive", duplicate_of_account="001W5G121A7SMRP58M")
    assert env.state.claim_retry(p["key"], "t")
    with pytest.raises(StaleProposal):
        apply_proposal(env.crm, env.state, p["key"], env.settings.raw)
    assert env.store.accounts["001U6RW32TY0WSXZZB"]["chow_current_account"] == ""


def test_unknown_create_outcome_needs_human_confirmation(env, monkeypatch):
    env.run()
    p = env.pending(kind="CREATE", title="Batavia")[0]
    real = env.crm.create_account

    def commit_then_lose(fields):
        out = real(fields)
        acc = env.store.accounts[out["account_id"]]
        acc.update(note="", billing_street="1 Moved Rd")        # marker gone and address changed
        raise RuntimeError("crash before logging")
    monkeypatch.setattr(env.crm, "create_account", commit_then_lose)
    with pytest.raises(RuntimeError):
        env.approve(p["key"])
    monkeypatch.setattr(env.crm, "create_account", real)
    assert env.state.claim_retry(p["key"], "t")
    with pytest.raises(Exception, match="outcome is unknown"):
        apply_proposal(env.crm, env.state, p["key"], env.settings.raw)
    assert sum(1 for x in env.store.log if x["op"] == "POST") == 1
    assert env.state.claim_retry(p["key"], "t")                  # person confirms after checking by hand
    apply_proposal(env.crm, env.state, p["key"], env.settings.raw, confirm_unresolved_create=True)
    assert sum(1 for x in env.store.log if x["op"] == "POST") == 2


def test_create_refused_if_a_phone_corroborated_account_appeared(env):
    env.run()
    p = env.pending(kind="CREATE", title="Batavia")[0]
    env.store.accounts["001HANDMADE0000003"] = dict(env.store.accounts["001NXP9X46CWEPSLSV"], account_id="001HANDMADE0000003",
        name="Bellhaven of Batavia", billing_street="PO Box 77", billing_city="Batavia", billing_zip="45999",
        phone="(231) 698-3092", note="")
    with pytest.raises(StaleProposal):
        env.approve(p["key"])


def test_known_occupant_that_became_the_successor_blocks_the_chow_create(env):
    twin = dict(env.store.accounts["001U6RW32TY0WSXZZB"], account_id="001TWIN00000000001", lifetime_revenue=0,
                outstanding_ar=0, note="")
    env.store.accounts[twin["account_id"]] = twin                # second copy known at planning time
    env.run()
    p = env.pending(kind="CHOW", title="Tiffin")[0]
    twin["parent_id"] = BH                                       # a rep moved that copy to Bellhaven meanwhile
    with pytest.raises(StaleProposal):
        env.approve(p["key"])


def test_rejected_move_stays_rejected_across_reparent_and_consolidate(env):
    env.run()
    c = env.pending(kind="CONSOLIDATE")[0]
    assert env.state.reject(c["key"], "t", "no", c["version"])
    for aid in ("0016KTS1UAWBRXS09J", "001B7XZAA3AFALS9GP"):
        env.store.accounts[aid].update(duplicate_of_account="001WR41PYNWXCAE2X4", status="Inactive")
    env.run()
    assert not env.pending(kind="REPARENT", title="Kettering")
    lima = env.pending(kind="REPARENT", title="Lima")[0]
    assert env.state.reject(lima["key"], "t", "no", lima["version"])
    env.store.accounts["001LIMACOPY0000001"] = dict(env.store.accounts["001LGFPBJY4N9MB6KL"], account_id="001LIMACOPY0000001",
                                                    name="Lima copy", note="")
    env.run()
    assert not env.pending(kind="CONSOLIDATE", slug="bellhaven-crossings-of-lima")


def test_explicitly_different_suites_do_not_match(env):
    _edit_page(env, "communities/bellhaven-of-wooster.html", "2301 Cleveland Road", "2301 Cleveland Road Suite 2")
    env.store.accounts["001W5G121A7SMRP58M"].update(billing_street="2301 Cleveland Road Suite 1", name="Other Tenant LLC",
                                                     phone="(330) 555-0123")
    env.run()
    assert env.pending(kind="CREATE", title="Wooster")


def test_survivor_address_fix_then_duplicate_retire_works_in_either_order(env):
    env.store.accounts["001UELXDAKFRKB8932"]["billing_street"] = "1420 Harbor Point Dr Suite 1"
    env.run()
    env.approve(env.pending(kind="ADDRESS", slug="bellhaven-of-port-clinton")[0]["key"])
    env.approve(env.pending(kind="DUPLICATE", slug="bellhaven-of-port-clinton")[0]["key"])
    assert env.store.accounts["001JD2MWRA74LTSN24"]["status"] == "Inactive"


def test_finish_retiring_covers_needs_review_and_chains(env):
    a = env.store.accounts
    a["001JD2MWRA74LTSN24"].update(duplicate_of_account="001UELXDAKFRKB8932", status="Needs Review")
    a["0011AB44D05WLA9HTX"].update(duplicate_of_account="00159PL81N38KM4FHM", status="Active")   # A -> B
    a["00159PL81N38KM4FHM"].update(duplicate_of_account="001U1750VLVJAGG1S5", status="Inactive")  # B -> C (live)
    env.run()
    fins = env.pending(title="Finish retiring")
    assert {p["ops"][0]["account_id"] for p in fins} == {"001JD2MWRA74LTSN24", "0011AB44D05WLA9HTX"}
    for p in fins:
        env.approve(p["key"])
    assert a["001JD2MWRA74LTSN24"]["status"] == "Inactive" and a["0011AB44D05WLA9HTX"]["status"] == "Inactive"


# ---------------------------------------------------------------- connection errors, closing failed proposals, suites
def test_connect_error_on_create_needs_no_human_confirmation(env, monkeypatch):
    env.run()
    p = env.pending(kind="CREATE", title="Batavia")[0]
    real = env.crm.http.request
    state = {"done": False}

    def refuse_once(method, url, **kw):
        if method == "POST" and not state["done"]:
            state["done"] = True
            raise httpx.ConnectError("connection refused", request=httpx.Request(method, url))
        return real(method, url, **kw)
    monkeypatch.setattr(env.crm.http, "request", refuse_once)
    with pytest.raises(Exception):
        env.approve(p["key"])
    assert env.state.claim_retry(p["key"], "t")
    apply_proposal(env.crm, env.state, p["key"], env.settings.raw)          # no confirmation needed
    assert len([a for a in env.store.accounts.values() if a["name"] == "Bellhaven of Batavia"]) == 1


def test_failed_proposal_can_be_closed_and_replanned(env, monkeypatch):
    env.run()
    p = env.pending(kind="CREATE", title="Batavia")[0]
    real = env.crm.create_account

    def commit_then_lose(fields):
        out = real(fields)
        env.store.accounts[out["account_id"]].update(note="", billing_city="Batavia Township")
        raise RuntimeError("crash before logging")
    monkeypatch.setattr(env.crm, "create_account", commit_then_lose)
    with pytest.raises(RuntimeError):
        env.approve(p["key"])
    monkeypatch.setattr(env.crm, "create_account", real)
    assert env.state.close_failed(p["key"], "t", "I checked: the account exists")
    env.run()
    fix = env.pending(slug="bellhaven-of-batavia")
    assert [x["kind"] for x in fix] == ["ADDRESS"] and fix[0]["ops"][0]["fields"] == {"billing_city": "Batavia"}


def test_conflicting_suites_do_not_match_even_with_the_same_name(env):
    _edit_page(env, "communities/bellhaven-of-wooster.html", "2301 Cleveland Road", "2301 Cleveland Road Suite 2")
    env.store.accounts["001W5G121A7SMRP58M"]["billing_street"] = "2301 Cleveland Road Suite 1"
    env.run()
    assert not env.pending(kind="ADDRESS", slug="bellhaven-of-wooster")
