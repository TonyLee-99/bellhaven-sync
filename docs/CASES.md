# Case table (expected outcome per location, derived from the initial CRM snapshot and the website)

The executable version of this table is `tests/test_end_to_end.py`.

Parent ids: Bellhaven `0015QAPLGS3FVYEEEM`, Harborview `001FJZYHR7MLFMNPLL`, Cedar Trail `001FWSQ30SFW6S7604`, Millstone `001YRHHXQ5HJ0TCL2U`, Juniper Point `001DAAUWV2J3SHQJ34`, Stonebridge `00139TNDS8HNLUZ5A6`.
Care-type mapping website -> CRM: Short-Term Rehabilitation & Nursing -> Skilled Nursing; Assisted Living -> Assisted Living; Memory Support -> Memory Care. No conflicts after mapping (CRM single value is always one of the site's offerings).
24 of 35 website phone area codes differ from the facility's state - not used as a validation rule; exact phone equality is used only as supporting evidence.

## A. Website locations (35)

| # | Website location | CRM match | Class | Writes | Rev/AR | Notes |
|---|---|---|---|---|---|---|
| 1 | Bellhaven at Sycamore Ridge (Miamisburg OH) | 00191H7JR16471SZ34 "Bellhaven of Sycamore Ridge" | fix: name | name -> website | 0/0 | cosmetic name variant |
| 2 | Bellhaven Court of Altoona (PA) | 0011D2P3CWSHMF81C8 | confident | none | 61000/0 | Blvd vs Boulevard only |
| 3 | Bellhaven Crossings of Lima (OH) | 001LGFPBJY4N9MB6KL (Harborview) | fix: parent | parent -> Bellhaven (direct; AR=0) | 47000/0 | Harborview acquisition |
| 4 | Bellhaven Healthcare Centre of Ashland | 001RJ0D7Z5MAZN5HX5 "Bellhaven Health Care Center of Ashland" | fix: name | name | 0/0 | |
| 5 | Bellhaven Meadows of Findlay | 001UKEFGADQ8YCZ4YM (no parent) | fix: parent | parent -> Bellhaven (direct; AR=0) | 22000/0 | not in list pages, homepage only |
| 6 | Bellhaven of Ashtabula | 001NXP9X46CWEPSLSV (billing PO Box 517) | confident | none (PO Box is a valid billing address; kept) | 0/0 | matched by name+city+state+zip+exact phone |
| 7 | Bellhaven of Carlisle (PA) | none (decoy: Bellhaven of New Carlisle OH) | new | create under Bellhaven | - | |
| 8 | Bellhaven of Batavia (OH) | none | new | create | - | |
| 9 | Bellhaven of Chagrin Falls | 001RJU1X4NBWC1Q0G7 "Riverbend Manor Care Center" (already Bellhaven) | fix: name (rebrand) | name | 0/0 | address match |
| 10 | Bellhaven of Chesterton (IN) | 0013NUZQHQUEZ8DXEG "Chesterton Senior Commons" | fix: name | name | 0/0 | NW vs Northwest; phone equal |
| 11 | Bellhaven of Defiance | 001R4PXTUD9R6FNN8V | confident | none | 66000/9500 | already Bellhaven -> no move, SOP not triggered |
| 12 | Bellhaven of Goshen | 001JQER1RMYKNRLNME | confident | none | | |
| 13 | Bellhaven of Kalamazoo | 001NSHWKQD0YJLPQ20 | confident | none | | |
| 14 | Bellhaven of Kettering | 3 records at 3313 Wilmington Pike: 001WR41PYNWXCAE2X4 Kettering Care Centre (Harborview), 0016KTS1UAWBRXS09J Kettering Nursing & Rehabilitation (no parent), 001B7XZAA3AFALS9GP Kettering Senior Campus (Cedar Trail, "Pk") | fix + dup | survivor 001WR41PYNWXCAE2X4 -> parent Bellhaven + name; 0016KTS1UAWBRXS09J, 001B7XZAA3AFALS9GP -> duplicate_of=survivor, Inactive | all 0/0, no contacts | survivor: documented predecessor (Harborview) + address typed exactly as on site |
| 15 | Bellhaven of Maplewood | 001L3LGR6W69J8DYYV | confident | none | | decoy Maplewood Senior Care Center (Stonebridge, other address) |
| 16 | Bellhaven of Marietta | 001A34WFSUYHCRBLFT (Cedar Trail) | CHOW | leave old; create new under Bellhaven; old.chow_current_account=new | 51250/3800 | |
| 17 | Bellhaven of Marion | 0015L38R1Z5947ZZP7 | confident | none | | |
| 18 | Bellhaven of Meadville | 001KC1NG7EZDEKKA3T | confident | none | 87000/1000 | already Bellhaven |
| 19 | Bellhaven of New Carlisle (OH) | 001AQGPZ5G5VRES536 | confident | none | | not Carlisle PA |
| 20 | Bellhaven of Owosso | 2 records under Bellhaven: 001EGU7BMJ942ZTRE6 (address+phone identical to site, Administrator contact) / 001QU150PM4Z15UA71 | dup | 001QU150...: duplicate_of=001EGU7..., Inactive | 0/0 both | |
| 21 | Bellhaven of Port Clinton | 001UELXDAKFRKB8932 (Bellhaven) + 001JD2MWRA74LTSN24 Harborview Nursing & Rehab of Port Clinton | dup | Harborview copy: duplicate_of, Inactive | 0/0 | |
| 22 | Bellhaven of Portsmouth | 001CF3LDWVRGL09P4F zip 45626 | fix: zip | billing_zip 45626 -> 45662 | | digit transposition; phone equal |
| 23 | Bellhaven of Saline | 001MVVAPY6U5TVYRC4 | confident | none | | |
| 24 | Bellhaven of Tiffin | 001U6RW32TY0WSXZZB (Cedar Trail) | CHOW | as Marietta | 84000/12400 | |
| 25 | Bellhaven of Wooster | 001W5G121A7SMRP58M | confident | none | | |
| 26 | Bellhaven of Zanesville | 001H1JMVZWP46D5VUF "Cedar Trail of Zanesville" (Cedar Trail) | fix: parent+name | parent -> Bellhaven (direct; 0/0), name | 0/0 | |
| 27 | Bellhaven Rehabilitation & Nursing of Grove City | 001TZCSTBZFM6K5BGN "Bellhaven Rehab and Nursing of Grove City" | fix: name | name | | |
| 28 | Bellhaven Shores of Erie | 001CVBBCSDM7YHN220 (Bellhaven) + 001BLYF02K97SZLZHH Harborview Shores of Erie | dup | Harborview copy: duplicate_of, Inactive | 0/0 | |
| 29 | Bellhaven Terrace of Akron | 0019Y4J61ZBG4R00CD | confident | none | 101000/9000 | already Bellhaven |
| 30 | Bellhaven Willow Creek (Portage MI) | 0017MN2JYAJBDS8WQZ "Sunny Acres Retirement Home" | fix: name | name | | |
| 31 | Bellhaven Woods of Toledo | 0014DHACE6WQU3RMM5 | confident | none | | |
| 32 | The Arbors at Bellhaven - Dayton | 0010KJFP601YNTZVDA "Arbors at Bellhaven Dayton" | fix: name | name | | |
| 33 | Bellhaven Gardens of Monroe (MI) | 001U1750VLVJAGG1S5 (Bellhaven) + 0011AB44D05WLA9HTX Cedar Trail of Monroe + 00159PL81N38KM4FHM Monroe Gardens Care Center (Harborview) | dup | both losers: duplicate_of, Inactive | 0/0 | |
| 34 | Amberly Manor (Hudson OH) | none (4 Amberly decoys elsewhere) | new | create | | |
| 35 | Bellhaven at Union Square (New Albany OH) | none (decoy: Union Square Senior Living, Juniper Point, 240 Market St, other phone) | new | create | | |

## B. CRM accounts under Bellhaven not on the website

| account | Decision | Writes | Why |
|---|---|---|---|
| 001SXSF4ELF0Z2LGDM Bellhaven of Sandusky (130000/5200) | CHOW to Millstone (link to existing successor) | leave as is except chow_current_account = 0017JP8Z1UQ763BVK3 (Millstone Care of Sandusky, already under Millstone 001YRHHXQ5HJ0TCL2U at the same address); that record is not changed | moved to Millstone (same-address Millstone record). The account the SOP asks for already exists, so no second one is created. Alternative `existing_successor: create`: new Millstone account, old one linked to it, existing copy retired as its duplicate |
| 00116ETS45BL7DTQP7 Bellhaven Care Center of Alliance (0/0) | not on site, no successor evidence | status Needs Review + note | can't tell closed vs sold vs site omission |
| 0016PVXH4B25HWR7QE Bellhaven of Coldwater (0/0) | same | status Needs Review + note | |

## C. Untouched on purpose
Decoys (Amberly x4, Union Square Senior Living, Maplewood Senior Care Center) and every account not
listed above. Parent accounts are not changed: after reconciliation Harborview has no Active facility children (3 Inactive duplicate
copies keep their historical parent link). Contacts are not moved (e.g. the Owosso copy keeps its Admissions Director contact).

## Expected counts
Accounts created 6 (Carlisle, Batavia, Amberly Manor, Union Square + Marietta-new, Tiffin-new) -> 127 accounts total.
Re-parented 4 (Lima, Findlay, Zanesville, Kettering survivor). Renamed 9 (Chagrin Falls, Chesterton, Willow Creek,
Zanesville, Kettering, Sycamore Ridge, Ashland, Grove City, Dayton). Duplicates retired 7 (Monroe x2, Kettering x2,
Owosso, Port Clinton, Erie). CHOW links 3 (Marietta, Tiffin, Sandusky). Needs Review 2 (Alliance, Coldwater).
Zip fix 1 (Portsmouth). Proposals (one per logical change): CHOW 3, REPARENT 3 (Lima, Findlay, Zanesville),
CONSOLIDATE 1 (Kettering: re-parent survivor + retire 2 copies), RENAME 9, ADDRESS 1, CREATE 4, DUPLICATE 5, NOT_ON_SITE 2 = 28.
After approval: 35 Active non-duplicate accounts under Bellhaven (one per website location) plus the protected
Sandusky billing account (Active, linked via chow_current_account).
