"""Full pipeline against the mock CRM: scrape -> propose -> approve -> verify the CRM end state.

The expected end state is the per-location case table in docs/CASES.md, worked out from the raw
website and CRM data before the code existed.
"""
import pytest

from bellhaven_sync.executor import NotApproved, StaleProposal, apply_proposal

BH = "0015QAPLGS3FVYEEEM"
MILLSTONE_SANDUSKY = "0017JP8Z1UQ763BVK3"

EXPECTED_PATCHES = {
    # re-parent (no billing lock)
    "001LGFPBJY4N9MB6KL": {"parent_id": BH},                                         # Lima (47k rev, 0 AR)
    "001UKEFGADQ8YCZ4YM": {"parent_id": BH},                                         # Findlay (no parent)
    "001H1JMVZWP46D5VUF": {"parent_id": BH, "name": "Bellhaven of Zanesville"},
    "001WR41PYNWXCAE2X4": {"parent_id": BH, "name": "Bellhaven of Kettering"},       # Kettering survivor
    # renames
    "001RJU1X4NBWC1Q0G7": {"name": "Bellhaven of Chagrin Falls"},
    "0013NUZQHQUEZ8DXEG": {"name": "Bellhaven of Chesterton"},
    "0017MN2JYAJBDS8WQZ": {"name": "Bellhaven Willow Creek"},
    "00191H7JR16471SZ34": {"name": "Bellhaven at Sycamore Ridge"},
    "001RJ0D7Z5MAZN5HX5": {"name": "Bellhaven Healthcare Centre of Ashland"},
    "001TZCSTBZFM6K5BGN": {"name": "Bellhaven Rehabilitation & Nursing of Grove City"},
    "0010KJFP601YNTZVDA": {"name": "The Arbors at Bellhaven - Dayton"},
    # field fix
    "001CF3LDWVRGL09P4F": {"billing_zip": "45662"},                                  # Portsmouth
    # duplicates -> Inactive + pointer to survivor
    "0011AB44D05WLA9HTX": {"duplicate_of_account": "001U1750VLVJAGG1S5", "status": "Inactive"},
    "00159PL81N38KM4FHM": {"duplicate_of_account": "001U1750VLVJAGG1S5", "status": "Inactive"},
    "0016KTS1UAWBRXS09J": {"duplicate_of_account": "001WR41PYNWXCAE2X4", "status": "Inactive"},
    "001B7XZAA3AFALS9GP": {"duplicate_of_account": "001WR41PYNWXCAE2X4", "status": "Inactive"},
    "001QU150PM4Z15UA71": {"duplicate_of_account": "001EGU7BMJ942ZTRE6", "status": "Inactive"},
    "001JD2MWRA74LTSN24": {"duplicate_of_account": "001UELXDAKFRKB8932", "status": "Inactive"},
    "001BLYF02K97SZLZHH": {"duplicate_of_account": "001CVBBCSDM7YHN220", "status": "Inactive"},
    # not on website
    "00116ETS45BL7DTQP7": {"status": "Needs Review"},                                # Alliance
    "0016PVXH4B25HWR7QE": {"status": "Needs Review"},                                # Coldwater
    # CHOW: protected old accounts change ONLY chow_current_account
}
# Sandusky (left Bellhaven; Millstone record already at its address; revenue + AR), default "link" mode:
# old account untouched except chow_current_account -> the EXISTING Millstone account, which stays as it is.
# ("create" mode is covered in test_regressions.test_create_mode_for_existing_successor.)
CHOW_TO_NEW = {"001A34WFSUYHCRBLFT": "Bellhaven of Marietta", "001U6RW32TY0WSXZZB": "Bellhaven of Tiffin"}
EXPECTED_NEW = {"Amberly Manor": ("4390 Darrow Rd", "Hudson", "OH", "44236", "Assisted Living"),
                "Bellhaven at Union Square": ("118 Union Square Dr", "New Albany", "OH", "43054", "Assisted Living"),
                "Bellhaven of Batavia": ("2000 Hospital Dr", "Batavia", "OH", "45103", "Skilled Nursing"),
                "Bellhaven of Carlisle": ("640 Walnut Bottom Rd", "Carlisle", "PA", "17015", "Skilled Nursing"),
                "Bellhaven of Marietta": ("805 Colegate Dr", "Marietta", "OH", "45750", "Skilled Nursing"),
                "Bellhaven of Tiffin": ("45 St Lawrence Dr", "Tiffin", "OH", "44883", "Skilled Nursing")}
MILLSTONE = "001YRHHXQ5HJ0TCL2U"
DECOYS = ["0015D74ZLRY810RGY5", "001UXEM0PCSUVRAYPF", "001DPANQQTRR1LXFY4", "00192CFL38Y0U4Q887",
          "001GNU41AVXZRLLJ9P", "0019M1Z45PGV2DJ971", "001NXP9X46CWEPSLSV",
          "001AQGPZ5G5VRES536", "0019Y4J61ZBG4R00CD", "001R4PXTUD9R6FNN8V", "001KC1NG7EZDEKKA3T"]
VOLATILE = {"updated_at", "parent_name", "note"}
EXPECTED_COUNTS = {"CHOW": 3, "REPARENT": 3, "CONSOLIDATE": 1, "RENAME": 9, "ADDRESS": 1, "CREATE": 4,
                   "DUPLICATE": 5, "NOT_ON_SITE": 2}


def _strip(d):
    return {k: v for k, v in d.items() if k not in VOLATILE}


def test_scrape_finds_all_35_including_homepage_only(env):
    locs, stats = env.scraper.scrape()
    assert len(locs) == 35 and stats["directory_pages"] == 3 and stats["directory_finished"]
    assert not stats["failed_pages"] and not stats["failed_sources"]
    findlay = next(l for l in locs if l.slug == "bellhaven-meadows-of-findlay")
    assert not findlay.listed_in_directory
    erie = next(l for l in locs if l.slug == "bellhaven-shores-of-erie")
    assert erie.care_offerings == ["Assisted Living", "Memory Support"]


def test_pipeline_itself_writes_nothing(env):
    before = {k: dict(v) for k, v in env.store.accounts.items()}
    stats = env.run()
    assert stats["proposals_generated"] == EXPECTED_COUNTS and stats["queue"]["new"] == 28
    assert stats["crawl"]["complete"]["ok"]
    assert env.store.accounts == before and env.store.log == []


def test_end_state_matches_answer_key(env):
    env.run()
    env.approve_all()
    final = env.store.accounts
    for aid, fields in EXPECTED_PATCHES.items():
        for k, v in fields.items():
            assert final[aid][k] == v, (aid, k, final[aid][k], v)
    for aid, new_name in CHOW_TO_NEW.items():           # protected CHOW accounts: only the link changed
        old, seed = final[aid], env.seed_accounts[aid]
        new = final[old["chow_current_account"]]
        assert new["name"] == new_name and new["parent_id"] == BH and new["status"] == "Active"
        assert new["lifetime_revenue"] == 0 and new["outstanding_ar"] == 0
        assert {k: v for k, v in old.items() if k not in VOLATILE | {"chow_current_account"}} == \
               {k: v for k, v in seed.items() if k not in VOLATILE | {"chow_current_account"}}
        assert old["note"] == seed["note"]
    sandusky = final["001SXSF4ELF0Z2LGDM"]
    assert sandusky["parent_id"] == BH and sandusky["status"] == "Active" and sandusky["note"] == ""
    assert sandusky["chow_current_account"] == MILLSTONE_SANDUSKY
    assert _strip(final[MILLSTONE_SANDUSKY]) == _strip(env.seed_accounts[MILLSTONE_SANDUSKY])
    created = [a for a in final.values() if a["created_by_candidate"]]
    assert sorted(a["name"] for a in created) == sorted(EXPECTED_NEW)
    for a in created:
        street, city, state, zip_, care = EXPECTED_NEW[a["name"]]
        parent = BH
        assert (a["billing_street"], a["billing_city"], a["billing_state"], a["billing_zip"], a["care_type"],
                a["parent_id"], a["status"]) == (street, city, state, zip_, care, parent, "Active")
    for aid in DECOYS:
        assert _strip(final[aid]) == _strip(env.seed_accounts[aid]), aid
    touched = set(EXPECTED_PATCHES) | set(CHOW_TO_NEW) | {a["account_id"] for a in created} | {"001SXSF4ELF0Z2LGDM", MILLSTONE_SANDUSKY}
    for aid, seed in env.seed_accounts.items():
        if aid not in touched:
            assert final[aid] == seed, aid                 # untouched means untouched, note included
    live_bh = [a for a in final.values() if a["parent_id"] == BH and a["status"] == "Active"
               and not a["duplicate_of_account"] and not a["chow_current_account"]]
    assert len(live_bh) == 35 and len(final) == 127
    stats = env.run()
    assert stats["queue"]["new"] == 0 and stats["queue"]["regressed"] == 0 and not env.pending()


def test_rejections_stay_rejected_across_restarts(env):
    from bellhaven_sync.state import State
    env.run()
    rej = env.pending(kind="NOT_ON_SITE")[0]
    assert env.state.reject(rej["key"], "test", "rep confirmed it is open", rej["version"])
    env.approve_all()
    env.state = State(env.settings.db_path)                # restart
    stats = env.run()
    assert stats["queue"]["new"] == 0 and stats["queue"]["already_decided"] == 1 and not env.pending()


def test_executor_refuses_unclaimed(env):
    env.run()
    p = env.pending()[0]
    with pytest.raises(NotApproved):
        apply_proposal(env.crm, env.state, p["key"])
    assert env.store.log == []


def test_second_approval_is_refused(env):
    env.run()
    p = env.pending(kind="CREATE")[0]
    assert env.state.approve_and_claim(p["key"], "a", "", p["version"])
    assert not env.state.approve_and_claim(p["key"], "b", "", p["version"])     # second tab
    assert not env.state.reject(p["key"], "b", "x", p["version"])
    apply_proposal(env.crm, env.state, p["key"])
    assert len([a for a in env.store.accounts.values() if a["created_by_candidate"]]) == 1


def test_approval_bound_to_the_version_the_reviewer_saw(env):
    env.run()
    p = env.pending(kind="CREATE")[0]
    assert not env.state.approve_and_claim(p["key"], "a", "", "not-the-current-version")
    assert env.state.get(p["key"])["status"] == "pending"


def test_stale_proposal_writes_nothing_and_is_replanned(env):
    env.run()
    p = env.pending(kind="REPARENT", title="Lima")[0]
    env.store.accounts["001LGFPBJY4N9MB6KL"]["outstanding_ar"] = 500     # billing changed after the proposal
    with pytest.raises(StaleProposal):
        env.approve(p["key"])
    assert env.state.get(p["key"])["status"] == "superseded"
    assert env.store.accounts["001LGFPBJY4N9MB6KL"]["parent_id"] != BH
    env.run()                                                   # now revenue AND AR -> CHOW, not a direct move
    assert {x["kind"] for x in env.pending(slug="bellhaven-crossings-of-lima")} == {"CHOW"}


def test_half_finished_chow_resumes_without_second_create(env, monkeypatch):
    env.run()
    p = env.pending(kind="CHOW", title="Tiffin")[0]
    real = env.crm.patch_account
    calls = {"n": 0}

    def flaky(aid, fields):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("network dropped")
        return real(aid, fields)

    monkeypatch.setattr(env.crm, "patch_account", flaky)
    with pytest.raises(RuntimeError):
        env.approve(p["key"])
    assert env.state.get(p["key"])["status"] == "failed"
    env.run()                                                   # daily run in between: in-flight records untouched
    assert not env.pending(slug="bellhaven-of-tiffin")
    assert env.state.claim_retry(p["key"], "test")
    apply_proposal(env.crm, env.state, p["key"])
    tiffins = [a for a in env.store.accounts.values() if a["name"] == "Bellhaven of Tiffin" and a["created_by_candidate"]]
    assert len(tiffins) == 1
    assert env.store.accounts["001U6RW32TY0WSXZZB"]["chow_current_account"] == tiffins[0]["account_id"]


def test_incomplete_crawl_draws_no_absence_conclusions(env, monkeypatch):
    real = env.scraper.parse_detail

    def broken(slug, card):
        if slug == "bellhaven-of-wooster":
            raise ValueError("page layout changed")
        return real(slug, card)

    env.run()
    flagged = {p["key"] for p in env.pending(kind="NOT_ON_SITE")}
    monkeypatch.setattr(env.scraper, "parse_detail", broken)
    stats = env.run()
    assert stats["crawl"]["complete"]["ok"] is False
    assert "NOT_ON_SITE" not in stats["proposals_generated"]
    assert {p["key"] for p in env.pending(kind="NOT_ON_SITE")} == flagged      # kept, not superseded


def test_remote_hosts_are_refused_without_opt_in(monkeypatch):
    from bellhaven_sync.config import RemoteNotAllowed, guard_remote
    monkeypatch.delenv("ALLOW_REMOTE", raising=False)
    with pytest.raises(RemoteNotAllowed):
        guard_remote("https://example.com/api/v1")
    monkeypatch.setenv("ALLOW_REMOTE", "1")
    guard_remote("https://example.com/api/v1")
