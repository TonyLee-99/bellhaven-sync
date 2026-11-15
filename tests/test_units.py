from bellhaven_sync.normalize import house_number, is_po_box, norm_street, zip5
from bellhaven_sync.plan import has_billing_lock, names_equivalent

CFG = {"sop": {"revenue_field": "lifetime_revenue", "ar_field": "outstanding_ar"}}


def test_street_normalization_variants_are_equal():
    assert norm_street("1250 NW Franklin Street") == norm_street("1250 Northwest Franklin St")
    assert norm_street("3313 Wilmington Pk") == norm_street("3313 Wilmington Pike")
    assert norm_street("199 Barks Rd W") == norm_street("199 Barks Road West")
    assert norm_street("1125 Logan Blvd") == norm_street("1125 Logan Boulevard")
    assert norm_street("4930 W Lake Rd") == norm_street("4930 West Lake Road")
    assert norm_street("875 Elm St") != norm_street("640 Walnut Bottom Rd")


def test_small_helpers():
    assert house_number("1420 Harbor Point Dr") == "1420"
    assert is_po_box("PO Box 517") and is_po_box("P.O. Box 9") and not is_po_box("3156 W Prospect Rd")
    assert zip5("45662-1234") == "45662"


def test_names_equivalent_only_ignores_formatting():
    assert names_equivalent("Bellhaven Rehab and Nursing of Grove City", "Bellhaven Rehabilitation & Nursing of Grove City")
    assert names_equivalent("Bellhaven Health Care Center of Ashland", "Bellhaven Healthcare Centre of Ashland")
    assert names_equivalent("Arbors at Bellhaven Dayton", "The Arbors at Bellhaven - Dayton")
    assert not names_equivalent("Cedar Trail of Zanesville", "Bellhaven of Zanesville")
    assert not names_equivalent("Bellhaven of Carlisle", "Bellhaven of New Carlisle")


def test_sop_truth_table():
    def lock(rev, ar):
        return has_billing_lock({"lifetime_revenue": rev, "outstanding_ar": ar}, CFG)
    assert lock(1, 1) is True
    assert lock(84000, 12400) is True
    assert lock(47000, 0) is False      # Lima: revenue, no AR -> direct re-parent
    assert lock(0, 500) is False        # AR without revenue history -> direct re-parent
    assert lock(0, 0) is False


def test_unit_designators_do_not_change_the_street():
    from bellhaven_sync.match import street_parts
    def core(s):
        d = street_parts(s)
        d.pop("unit")
        return d
    assert core("2301 Cleveland Rd Suite 1") == core("2301 Cleveland Road")
    assert core("2301 Cleveland Rd #4") == core("2301 Cleveland Rd")
    assert street_parts("2301 Cleveland Rd Suite 1")["unit"] == "1"
