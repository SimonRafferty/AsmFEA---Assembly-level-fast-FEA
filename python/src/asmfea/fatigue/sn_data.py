"""AsmFEA's built-in S-N data for parent material (D111): looked up for a material when neither the study (the Fatigue
tab) nor the SolidWorks material itself has an S-N curve.

1. Measured (mean curves, smooth specimens, R = -1), matched by the material's name: MIL-HDBK-5J (US DoD, 2003,
   approved for public release), whose fits log N = A - B log(S_eq - C), S_eq = S_max (1 - R)^m are evaluated at
   R = -1 (S_max = the amplitude) from 1e3 to 1e8 cycles (spikes / research notes, 2026-10-17; 6061-T6 checked
   against the handbook's equation: 138.2 MPa at 1e6).
2. Estimated from the material's tensile strength Rm (the SolidWorks material's), by its family:
   - steels (unalloyed and low-alloy): the Uniform Material Law (Baeumel & Seeger 1990), sigma_a = 1.50 Rm (2N)^-0.087;
   - wrought aluminium: the Uniform Material Law, sigma_a = 1.67 Rm (2N)^-0.095 (checked: 105 MPa at 1e7 for
     6061-T6 against MIL-HDBK-5J's 109);
   - stainless steels (the UML is poor for them): the FKM guideline's fatigue-limit ratio 0.40 Rm (axial, R = -1) at
     1e6 cycles, with the steel slope: sigma_a = 0.40 Rm (2N / 2e6)^-0.087.
   Sources: Bressan et al., Frattura ed Integrita Strutturale 47 (2019) (the UML's constants); PTC Creo help (its
   scope); the FKM ratios from pylife's constants (Bosch, Apache-2.0).
Both are MEAN curves of polished specimens: the design curve is the mean x SN_DESIGN_FACTOR on stress, times a
surface factor (Marin's, from Shigley, for steel: hot-rolled by default for structural steel); aluminium and stainless
take 1 (the UML's surface option is not for aluminium; state the finish in the source if it matters).
"""
from __future__ import annotations

import math
import re

from .. import defaults

# MIL-HDBK-5J smooth-specimen curves at R = -1: stress amplitude, MPa, at 1e3, 1e4, 1e5, 1e6, 1e7, 1e8 cycles
_N = [1e3, 1e4, 1e5, 1e6, 1e7, 1e8]
MEASURED = [
    (r"6061[\s-]*T6", "MIL-HDBK-5J Fig. 3.6.2.2.8 (6061-T6 rod and bar, unnotched, mean)", [279, 221, 175, 138, 109, 87]),
    (r"7075[\s-]*T6.*sheet|sheet.*7075[\s-]*T6", "MIL-HDBK-5J Fig. 3.7.6.1.8(d) (7075-T6 sheet, unnotched, mean)",
     [544, 366, 246, 165, 111, 75]),
    (r"7075[\s-]*T6", "MIL-HDBK-5J Fig. 3.7.6.1.8(a) (7075-T6 rod and bar, unnotched, mean)", [454, 349, 271, 213, 170, 138]),
]

# Marin's surface factor, ka = a Rm^b (Rm in MPa; Shigley; two sources)
MARIN = {"ground": (1.58, -0.085), "machined": (4.51, -0.265), "hot_rolled": (57.7, -0.718), "as_forged": (272.0, -0.995)}


def family(name: str, E: float, density_t_mm3: float) -> str | None:
    """steel | stainless | aluminium | None, from the SolidWorks material's name and properties."""
    n = name.lower()
    rho = density_t_mm3 * 1e12                         # kg/m^3
    if 60000 <= E <= 80000 and 2500 <= rho <= 2900:
        return "aluminium"
    if 180000 <= E <= 220000 and 7600 <= rho <= 8100:
        if re.search(r"stainless|inox|\b30[1-9]\b|\b31[0-9]L?\b|\b316|\b304|1\.43|1\.44|duplex|17-4|aisi 3", n):
            return "stainless"
        return "steel"
    return None


def surface_factor(fam: str, finish: str | None, Rm: float) -> tuple[float, str]:
    """(factor, finish) for the family: steel by Marin (default hot-rolled), others 1 (polished)."""
    if fam != "steel":
        return 1.0, finish or "as tested (polished)"
    f = finish or "hot_rolled"
    if f == "polished" or f not in MARIN:
        return 1.0, "polished"
    a, b = MARIN[f]
    return min(1.0, a * Rm ** b), f


def builtin_curve(material, finish: str | None = None) -> dict | None:
    """The design curve AsmFEA has for this material (a tables-style curve dict), or None."""
    from . import tables
    name = material.name
    for pat, src, amps in MEASURED:
        if re.search(pat, name, re.I):
            fam = family(name, material.E, material.density) or "aluminium"
            k, fin = surface_factor(fam, finish, material.ultimate or 0.0)
            f = defaults.SN_DESIGN_FACTOR * k
            c = tables.user_points([[n, a] for n, a in zip(_N, amps)], f,
                                   f"{name}: measured S-N, built in ({src}) x {defaults.SN_DESIGN_FACTOR:g} design"
                                   + (f" x {k:.2f} surface ({fin})" if k != 1.0 else ""), src)
            return {**c, "estimated": False}
    fam = family(name, material.E, material.density)
    Rm = material.ultimate
    if fam is None or not Rm:
        return None
    k, fin = surface_factor(fam, finish, Rm)
    f = defaults.SN_DESIGN_FACTOR * k
    if fam == "steel":
        Sf, b, how = 1.50 * Rm, -0.087, "Uniform Material Law (Baeumel & Seeger 1990), steel"
    elif fam == "aluminium":
        Sf, b, how = 1.67 * Rm, -0.095, "Uniform Material Law (Baeumel & Seeger 1990), wrought aluminium"
    else:
        Sf, b, how = 0.40 * Rm * (2e6) ** 0.087, -0.087, "FKM fatigue-limit ratio 0.40 Rm at 1e6, steel slope (stainless)"
    c = tables.user_basquin(Sf, b, None, f, f"{name}: ESTIMATED from Rm {Rm:g} MPa ({how}) x {defaults.SN_DESIGN_FACTOR:g} "
                            f"design" + (f" x {k:.2f} surface ({fin})" if k != 1.0 else ""),
                            f"{how}; Bressan et al. 2019, FKM via pylife (estimate, not a measured curve)")
    return {**c, "estimated": True, "checked": False}
