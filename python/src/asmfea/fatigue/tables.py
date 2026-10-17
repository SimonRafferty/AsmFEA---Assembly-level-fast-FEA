"""S-N curves of the standards in the study's list (PLAN-FATIGUE.md, D102), with their sources.

Every curve is data from its standard. None of the standards is held in full (the user does not buy them, D102):
each row says where it was read and whether two sources agree (investigations/fatigue/STANDARDS.md). A curve is a
dict read by `asmfea.fatigue.life`:

- "two_slope": range S, S^m1 N = A1 down to the knee at N_knee; below it, at constant amplitude no damage (the
  constant-amplitude limit), under a spectrum the slope m2 (S^m2 N = A2) down to the cut-off at N_cut (none: no
  cut-off). BS 7608, PD 5500, EN 1993-1-9.
- "one_slope": range S, N = N_ref (S_ref / S)^m with no knee (EN 13001-3-1 6.2.1: the stress history parameter
  uses one slope).

Ranges are design values (mean - 2 SD, 97.7 % survival) and include residual stress; no mean-stress correction is
applied to them (each standard's rule for welded and as-rolled steel).
"""
from __future__ import annotations

import math

STANDARD_NAMES = {
    "bs7608": "BS 7608:2014+A1:2015",
    "pd5500": "PD 5500:2000 Annex C",
    "en13001_3_1": "EN 13001-3-1:2012+A1:2013",
    "en1993_1_9": "EN 1993-1-9:2005 (with the UK National Annex)",
}


def two_slope(A1: float, m1: float, N_knee: float, m2: float | None, N_cut: float | None, name: str,
              source: str, checked: bool, A2: float | None = None) -> dict:
    S_knee = (A1 / N_knee) ** (1.0 / m1)
    if m2 and A2 is None:
        A2 = S_knee ** m2 * N_knee
    S_cut = (A2 / N_cut) ** (1.0 / m2) if (m2 and N_cut) else None
    return {"kind": "two_slope", "A1": A1, "m1": m1, "N_knee": N_knee, "S_knee": S_knee, "m2": m2, "A2": A2,
            "N_cut": N_cut, "S_cut": S_cut, "name": name, "source": source, "checked": checked}


def one_slope(S_ref: float, m: float, N_ref: float, name: str, source: str, checked: bool) -> dict:
    return {"kind": "one_slope", "S_ref": S_ref, "m": m, "N_ref": N_ref, "name": name, "source": source,
            "checked": checked}


# --- BS 7608 / PD 5500 -------------------------------------------------------------------------------------------
# PD 5500:2000 Table C.1 (the user's copy): S^m N = A to 10^7, S^(m+2) N = A2 beyond (variable amplitude; the
# constant-amplitude limit at 10^7), E = 2.09e5. Its classes are BS 7608's; its "W" is BS 7608:2014's G2.
# BS 7608:2014 classes B and W1 from JOET "Comparison of fatigue provisions ... Part 1" Table 9 (log C2), one source.
_PD = "PD 5500:2000 Table C.1 (user's copy)"
_BS = {
    "B": two_slope(10 ** 15.0055, 4.0, 1e7, 6.0, None, "BS 7608 class B",
                   "BS 7608:2014 via JOET Comparison of fatigue provisions, Table 9", False),
    "C": two_slope(4.22e13, 3.5, 1e7, 5.5, None, "BS 7608 class C", _PD + "; JOET Table 9 agrees", True, A2=2.55e17),
    "D": two_slope(1.52e12, 3.0, 1e7, 5.0, None, "BS 7608 class D", _PD + "; JOET Table 9 agrees", True, A2=4.18e15),
    "E": two_slope(1.04e12, 3.0, 1e7, 5.0, None, "BS 7608 class E", _PD + "; JOET Table 9 agrees", True, A2=2.29e15),
    "F": two_slope(6.33e11, 3.0, 1e7, 5.0, None, "BS 7608 class F", _PD + "; JOET Table 9 agrees", True, A2=1.02e15),
    "F2": two_slope(4.31e11, 3.0, 1e7, 5.0, None, "BS 7608 class F2", _PD + "; JOET Table 9 agrees", True, A2=5.25e14),
    "G": two_slope(2.50e11, 3.0, 1e7, 5.0, None, "BS 7608 class G", _PD + "; JOET Table 9 agrees", True, A2=2.05e14),
    "G2": two_slope(1.58e11, 3.0, 1e7, 5.0, None, "BS 7608 class G2", _PD + " (its class W); JOET Table 9 agrees",
                    True, A2=9.77e13),
    "W1": two_slope(10 ** 10.9699, 3.0, 1e7, 5.0, None, "BS 7608 class W1",
                    "BS 7608:2014 via JOET Comparison of fatigue provisions, Table 9", False),
}
_PD5500 = {k: {**v, "name": v["name"].replace("BS 7608", "PD 5500")} for k, v in _BS.items() if k in ("C", "D", "E", "F", "F2", "G")}
_PD5500["W"] = {**_BS["G2"], "name": "PD 5500 class W"}


# --- EN 1993-1-9:2005 ---------------------------------------------------------------------------------------------
# Delta sigma_c at 2e6; m = 3 to 5e6 (Delta sigma_D = 0.737 Delta sigma_c), m = 5 to 1e8 (Delta sigma_L = 0.405
# Delta sigma_c), no damage below. Two sources agree (Nussbaumer, EPFL; Investmech).
_EN_SRC = "EN 1993-1-9:2005 7.1 / Figure 7.1 via Nussbaumer (EPFL) and Investmech; two sources agree"
EN1993_CATEGORIES = [160, 140, 125, 112, 100, 90, 80, 71, 63, 56, 50, 45, 40, 36]
_EN = {str(c): two_slope(2e6 * c ** 3, 3.0, 5e6, 5.0, 1e8, f"EN 1993-1-9 category {c}", _EN_SRC, True)
       for c in EN1993_CATEGORIES}


# --- EN 13001-3-1:2012+A1:2013 ------------------------------------------------------------------------------------
# Delta sigma_c at 2e6 with one slope m (6.2.1, Figure 8b). Table D.1, basic material of structural members (m = 5):
# rolled surfaces to EN 10163 class A3/C3 by yield strength; class A1/C1 surfaces 140 whatever the yield (user's copy).
_EN13001_SRC = "EN 13001-3-1:2012+A1:2013 Table D.1 (user's copy)"


def en13001_d1(fy: float, surface: str = "A3") -> dict:
    """Table D.1 detail 1.1, basic material, m = 5: Delta sigma_c from the yield strength (class A3/C3 surfaces)."""
    if surface == "A1":
        dsc = 140.0
    elif fy <= 220:
        dsc = 140.0
    elif fy <= 320:
        dsc = 160.0
    elif fy <= 500:
        dsc = 180.0
    else:
        dsc = 200.0
    return one_slope(dsc, 5.0, 2e6, f"EN 13001-3-1 Table D.1 basic material ({dsc:g})", _EN13001_SRC, True)


# Parent-material classes each standard offers for unwelded material (standard_class in study.fatigue.materials).
PARENT_CLASSES = {
    "bs7608": {"B": _BS["B"], "C": _BS["C"]},
    "pd5500": {"C": _PD5500["C"]},
    "en1993_1_9": {"160": _EN["160"], "140": _EN["140"], "125": _EN["125"]},
    "en13001_3_1": {"D.1": None, "D.1-A1": None},          # from the material's yield strength (en13001_d1)
}


def parent_class(standard: str, name: str, fy: float) -> dict | None:
    """The curve of a parent-material class of the standard, or None when the standard has no such class."""
    classes = PARENT_CLASSES.get(standard, {})
    if name not in classes:
        return None
    if standard == "en13001_3_1":
        return en13001_d1(fy, "A1" if name.endswith("A1") else "A3")
    return classes[name]


def user_basquin(Sf: float, b: float, endurance: float | None, factor: float, name: str, source: str) -> dict:
    """The user's curve, fully reversed amplitude sigma_a = Sf (2N)^b, its strength times factor (surface, size)."""
    return {"kind": "basquin", "Sf": Sf * factor, "b": b, "endurance": endurance * factor if endurance else None,
            "name": name, "source": source, "checked": True}


def user_points(points: list, factor: float, name: str, source: str) -> dict:
    """The user's curve as [N, amplitude] points, log-log interpolated, strength times factor."""
    pts = sorted((float(n), float(s) * factor) for n, s in points)
    return {"kind": "points", "logN": [math.log10(n) for n, _ in pts], "logS": [math.log10(s) for _, s in pts],
            "name": name, "source": source, "checked": True}


# --- welded joints (F3) -------------------------------------------------------------------------------------------
# W1 (Dong's structural stress, D33) is read on each standard's structural (hot-spot) stress curve:
# - BS 7608 / PD 5500: class D at the weld toe (BS 7608:2014 hot-spot method; the IIETA rail benchmark assesses
#   hot-spot stresses on class D as per BS 7608; class D's constants are PD 5500's, two sources);
# - EN 1993-1-9 Annex B: 112 (full-penetration butt, ground flush), 100 (butt as welded; non-load-carrying fillet)
#   from Nussbaumer (EPFL) Table 4; 90 (load-carrying fillet) not confirmed by a second source;
# - EN 13001-3-1 has no structural stress method (6.1): EN 1993-1-9 Annex B is used, and the report says so.
STRUCTURAL_DEFAULT = {"bs7608": "D", "pd5500": "D", "en1993_1_9": "90", "en13001_3_1": "90"}
STRUCTURAL_CHOICES = {"bs7608": ["C", "D", "E", "F"], "pd5500": ["C", "D", "E", "F"],
                      "en1993_1_9": ["112", "100", "90"], "en13001_3_1": ["112", "100", "90"]}
EN13001_CLASSES = [36, 40, 45, 50, 56, 63, 71, 80, 90, 100, 112, 125, 140, 160, 180, 200, 225, 250]  # Annex E, x 1.125


def structural_curve(standard: str, name: str | None) -> dict | None:
    """W1's curve for the study's standard (name None: the default), or None when it is not one of its choices."""
    name = name or STRUCTURAL_DEFAULT[standard]
    if name not in STRUCTURAL_CHOICES[standard]:
        return None
    if standard in ("bs7608", "pd5500"):
        c = (_BS if standard == "bs7608" else _PD5500)[name]
        return {**c, "name": c["name"] + " (structural stress at the weld toe)"}
    c = _EN[name]
    checked = name != "90"
    note = "" if checked else "; category 90 (load-carrying fillet) from one source only"
    via = " (EN 13001-3-1 has no structural stress method: EN 1993-1-9's is used)" if standard == "en13001_3_1" else ""
    return {**c, "name": f"EN 1993-1-9 Annex B category {name}{via}", "source": "EN 1993-1-9:2005 Annex B via Nussbaumer (EPFL) Table 4" + note,
            "checked": checked}


def welded_category(standard: str, name: str) -> dict | None:
    """W2's curve: a detail category or class of the study's standard for a joint's nominal stress range."""
    if standard == "bs7608":
        return _BS.get(name)
    if standard == "pd5500":
        return _PD5500.get(name)
    if standard == "en1993_1_9":
        return _EN.get(name)
    if standard == "en13001_3_1":
        dsc, _, m = name.partition("/")          # "71" (m = 3, welded) or "71/5"
        try:
            v, mm = float(dsc), float(m or 3)
        except ValueError:
            return None
        if v not in EN13001_CLASSES or mm not in (3.0, 5.0):
            return None
        return one_slope(v, mm, 2e6, f"EN 13001-3-1 detail category {v:g} (m = {mm:g})",
                         "EN 13001-3-1:2012+A1:2013 6.2.1, Annexes D, E, H (user's copy)", True)
    return None


def welded_choices(standard: str) -> list[str]:
    if standard == "bs7608":
        return list(_BS)
    if standard == "pd5500":
        return list(_PD5500)
    if standard == "en1993_1_9":
        return [str(c) for c in EN1993_CATEGORIES]
    return [f"{c}" for c in EN13001_CLASSES] + [f"{c}/5" for c in EN13001_CLASSES]


# --- bolts in tension (preloaded mode, F3) -------------------------------------------------------------------------
# EN 1993-1-9 Table 8.1 detail 14: bolts in tension, category 50 on the tensile stress area, ks = (30/d)^0.25 above
# M30 (two sources, STANDARDS.md). EN 13001-3-1 Table D.2 detail 2.6 (user's copy): bolts of grade 8.8 or better in
# tension, m = 3: rolled thread 71 up to M30, 63 above; machined thread 50. BS 7608's class X was not found, so BS 7608
# and PD 5500 studies use EN 1993-1-9's, and the report says so.
ISO_COARSE_PITCH = {3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75, 14: 2.0, 16: 2.0, 18: 2.5, 20: 2.5,
                    22: 2.5, 24: 3.0, 27: 3.0, 30: 3.5, 33: 3.5, 36: 4.0, 39: 4.0, 42: 4.5, 48: 5.0}


def stress_area(d: float) -> tuple[float, str]:
    """A bolt's tensile stress area (ISO 898-1: pi/4 (d - 0.9382 p)^2 with the coarse pitch), and how it was found."""
    k = int(round(d))
    if abs(d - k) < 1e-6 and k in ISO_COARSE_PITCH:
        return math.pi / 4 * (d - 0.9382 * ISO_COARSE_PITCH[k]) ** 2, f"M{k} coarse thread (ISO 898-1)"
    return math.pi / 4 * (0.9 * d) ** 2, "0.9 d (no ISO coarse size matches)"


def bolt_curve(standard: str, d: float, thread: str) -> dict:
    if standard == "en13001_3_1":
        dsc = 50.0 if thread == "machined" else (71.0 if d <= 30 else 63.0)
        return one_slope(dsc, 3.0, 2e6, f"EN 13001-3-1 Table D.2 detail 2.6, bolt in tension ({dsc:g}, {thread} thread)",
                         "EN 13001-3-1:2012+A1:2013 Table D.2 (user's copy)", True)
    ks = (30.0 / d) ** 0.25 if d > 30 else 1.0
    c = _EN["50"]
    s = 50.0 * ks
    via = "" if standard == "en1993_1_9" else f" ({STANDARD_NAMES[standard]} has no bolt class in hand: EN 1993-1-9's is used)"
    return {**two_slope(2e6 * s ** 3, 3.0, 5e6, 5.0, 1e8, f"EN 1993-1-9 detail 14, bolt in tension (50, ks {ks:.3f}){via}",
                        c["source"], True)}
