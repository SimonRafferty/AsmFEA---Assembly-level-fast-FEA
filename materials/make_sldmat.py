"""Write two SolidWorks material libraries (UTF-16 .sldmat, the format of the shipped libraries):
igus iglidur plain-bearing grades from igus' published data, and 304 / 304L stainless sheet and plate by supply
condition (EN 10088-2 minimum values, ASTM A666 tempers). Written beside this script; copied into the Custom
Materials folder separately."""
import json
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

HERE = Path(__file__).parent
OUT = HERE


HEAD = ('<?xml version="1.0" encoding="utf-16"?>\n'
        '<mstns:materials xmlns:mstns="http://www.solidworks.com/sldmaterials" '
        'xmlns:msdata="urn:schemas-microsoft-com:xml-msdata" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:sldcolorswatch="http://www.solidworks.com/sldcolorswatch" version="2008.03">\n'
        '  <curves id="curve0">\n    <point x="1.0" y="1.0" />\n    <point x="2.0" y="1.0" />\n'
        '    <point x="3.0" y="1.0" />\n  </curves>\n')
TAIL = '</mstns:materials>\n'

PLASTIC_SHADER = ('      <shaders>\n'
                  '        <pwshader2 path="\\plastic\\high gloss\\default plastic.p2m" name="default plastic" isNewShader="1" />\n'
                  '        <cgshader2 name="defaultplastic" />\n'
                  '      </shaders>\n')
STEEL_SHADER = ('      <shaders>\n'
                '        <pwshader2 name="polished steel" isNewShader="1" path="\\metal\\steel\\polished steel.p2m" />\n'
                '        <cgshader2 name="PolishedSteel" />\n'
                '        <pwshader name="stainless steel" />\n'
                '        <cgshader name="SteelAISI304" />\n'
                '      </shaders>\n')


def material(name, matid, description, source, appdata, props, shader, rgb, hatch):
    lines = [f'    <material name={quoteattr(name)} matid="{matid}" envdata="0" description={quoteattr(description)} '
             f'propertysource={quoteattr(source)} appdata={quoteattr(appdata)}>\n', shader,
             f'      <swatchcolor RGB="{rgb}">\n'
             '        <sldcolorswatch:Optical Ambient="1.000000" Transparency="0.000000" Diffuse="0.800000" '
             'Specularity="0.500000" Shininess="0.300000" Emission="0.000000" />\n      </swatchcolor>\n',
             f'      <xhatch name="{hatch}" angle="0.0" scale="1.0" />\n', '      <physicalproperties>\n']
    labels = {"EX": "Elastic Modulus", "NUXY": "Poisson's Ratio", "GXY": "Shear Modulus",
              "ALPX": "Thermal Expansion Coefficient", "DENS": "Density", "KX": "Thermal Conductivity",
              "C": "Specific Heat", "SIGXT": "Tensile Strength", "SIGYLD": "Yield Strength"}
    for k in ("EX", "NUXY", "GXY", "ALPX", "DENS", "KX", "C", "SIGXT", "SIGYLD"):
        if props.get(k) is not None:
            lines.append(f'        <{k} displayname="{labels[k]}" value="{props[k]:.6g}" />\n')
    lines.append('      </physicalproperties>\n    </material>\n')
    return "".join(lines)


def write(fname, classification, materials):
    text = HEAD + f'  <classification name={quoteattr(classification)}>\n' + "".join(materials) + \
        '  </classification>\n' + TAIL
    (OUT / fname).write_text(text, encoding="utf-16")
    print("wrote", OUT / fname, len(materials), "materials")


# ------------------------------------------------------------------ igus iglidur
COLOURS = {"yellow": "F2C200", "dark grey": "555555", "grey": "8C8C8C", "black": "202020", "charcoal": "3A3A3A",
           "brown": "7A4A2A", "white": "F0F0F0", "blue": "3060B0", "red": "B03030", "light grey": "C8C8C8"}


def poisson(e_mpa: float) -> tuple[float, str]:
    """igus publishes no Poisson's ratio and no base polymers. A judgement from the published stiffness and
    descriptions: heavily filled / fibre-reinforced grades (flexural modulus >= 5 GPa) about 0.35, as for
    glass- or carbon-filled engineering polymers; unreinforced, tougher grades (< 3 GPa: low friction, impact /
    vibration damping, food grades) about 0.40, as for unfilled PA, POM, PBT and PEEK; between, 0.38."""
    if e_mpa >= 5000:
        return 0.35, "about 0.35, typical of a filled / fibre-reinforced engineering polymer (flexural modulus >= 5 GPa)"
    if e_mpa < 3000:
        return 0.40, "about 0.40, typical of an unreinforced engineering polymer (flexural modulus < 3 GPa)"
    return 0.38, "about 0.38, between a filled and an unfilled engineering polymer (flexural modulus 3-5 GPa)"


data = json.loads((HERE / "igus_data.json").read_text(encoding="utf-8"))
igus, skipped = [], []
for k, g in enumerate(data):
    name = g["grade"]
    need = ("density_g_cm3", "modulus_MPa", "compressive_strength_MPa")
    if name.endswith("G300") or any(g.get(x) is None for x in need):
        skipped.append(name)
        continue
    E = g["modulus_MPa"]
    nu, why = poisson(E)
    colour = (g.get("colour") or "").lower()
    rgb = next((v for c, v in COLOURS.items() if c in colour), "B4B4B4")
    temps = f"{g.get('min_temp_C')} to +{g.get('max_long_term_temp_C')} C long term (+{g.get('max_short_term_temp_C')} short term)"
    app = (f"igus {name} plain bearing material. {g['description']}\n\n"
           f"Published (igus, retrieved 2026-10-10): flexural modulus {E} MPa (DIN 53457; igus give no tensile modulus), "
           f"flexural strength {g.get('strength_20C_MPa')} MPa, compressive strength {g['compressive_strength_MPa']} MPa, "
           f"max. recommended surface pressure {g.get('max_recommended_surface_pressure_MPa')} MPa, Shore D "
           f"{g.get('shore_d_hardness')}, application temperature {temps}, moisture absorption "
           f"{g.get('moisture_absorption_23C_50RH_pct_weight')} % (23 C / 50 % RH), max. "
           f"{g.get('max_water_absorption_pct_weight')} %.\n\n"
           "Entered as: Elastic Modulus = the flexural modulus; Tensile Strength = the flexural strength (igus publish "
           "no tensile strength); Yield Strength = the compressive strength (a bearing's governing strength; plastics "
           "have no yield point as metals do). Check bearings against the max. recommended surface pressure.\n"
           f"Poisson's ratio is not published: {why}, judged from igus' description and stiffness.\n\n"
           f"Source: {g['source_url']}")
    props = {"EX": E * 1e6, "NUXY": nu, "GXY": E * 1e6 / (2 * (1 + nu)), "DENS": g["density_g_cm3"] * 1000,
             "ALPX": g["thermal_expansion_1e-5_per_K"] * 1e-5 if g.get("thermal_expansion_1e-5_per_K") else None,
             "KX": g.get("thermal_conductivity_W_mK"), "SIGXT": (g.get("strength_20C_MPa") or 0) * 1e6 or None,
             "SIGYLD": g["compressive_strength_MPa"] * 1e6}
    short = (f"igus plain bearing; E = flexural, yield = compressive strength; p max "
             f"{g.get('max_recommended_surface_pressure_MPa')} MPa; nu {nu:.2f} estimated")
    igus.append(material(name, 9101 + k, short, "igus published data (igus.eu), retrieved 2026-10-10", app, props,
                         PLASTIC_SHADER, rgb, "ISO (plastic)"))
write("igus iglidur.sldmat", "igus iglidur", igus)
print("skipped:", skipped)

# ------------------------------------------------------------------ stainless 304 / 304L by supply condition
# EN 10088-2:2014 table 8 (austenitic, minimum values; C = cold-rolled strip <= 8 mm, H = hot-rolled strip <= 13.5 mm,
# P = hot-rolled plate <= 75 mm); ASTM A666 for the temper-rolled grades. Physical properties per EN 10088-1 annex.
COMMON = {"EX": 200e9, "NUXY": 0.30, "GXY": 200e9 / 2.6, "ALPX": 16.0e-6, "DENS": 7900, "KX": 15.0, "C": 500.0}
STEEL = [
    ("1.4301 (304) cold rolled, 2B", 230, 540,
     "EN 10088-2 1.4301 condition C (cold-rolled strip/sheet, usually 2B finish) annealed: Rp0.2 >= 230 MPa, Rm 540-750 MPa. "
     "The usual laser-cut sheet up to about 3-6 mm."),
    ("1.4301 (304) hot rolled plate, 1D", 210, 520,
     "EN 10088-2 1.4301 condition P (hot-rolled plate, 1D finish) annealed: Rp0.2 >= 210 MPa, Rm 520-720 MPa. "
     "The usual laser-cut plate from about 4-6 mm up."),
    ("1.4307 (304L) cold rolled, 2B", 220, 520,
     "EN 10088-2 1.4307 condition C (cold-rolled strip/sheet, 2B) annealed: Rp0.2 >= 220 MPa, Rm 520-700 MPa. "
     "Stock is often dual-certified 1.4301/1.4307: use these, the lower figures, unless the certificate says otherwise."),
    ("1.4307 (304L) hot rolled plate, 1D", 200, 500,
     "EN 10088-2 1.4307 condition P (hot-rolled plate, 1D) annealed: Rp0.2 >= 200 MPa, Rm 500-700 MPa."),
    ("AISI 304 1/4 hard (ASTM A666)", 515, 860,
     "ASTM A666 type 304, 1/4 hard temper (temper-rolled): yield >= 515 MPa (75 ksi), tensile >= 860 MPa (125 ksi). "
     "A special order, not normal laser-cutting stock; cold forming and welding reduce it locally."),
    ("AISI 304 1/2 hard (ASTM A666)", 760, 1035,
     "ASTM A666 type 304, 1/2 hard temper (temper-rolled): yield >= 760 MPa (110 ksi), tensile >= 1035 MPa (150 ksi). "
     "A special order, not normal laser-cutting stock; cold forming and welding reduce it locally."),
]
steel = []
for k, (name, ys, uts, text) in enumerate(STEEL):
    app = (text + "\n\nStrengths are the standard's guaranteed MINIMUM values; a 3.1 mill certificate usually shows "
           "higher actual values (annealed 304 commonly 260-320 MPa proof strength). Physical properties: E 200 GPa, "
           "nu 0.30, density 7900 kg/m3, expansion 16e-6 /K, conductivity 15 W/mK, specific heat 500 J/kgK "
           "(EN 10088-1 reference data).")
    props = dict(COMMON, SIGYLD=ys * 1e6, SIGXT=uts * 1e6)
    steel.append(material(name, 9201 + k, f"min. Rp0.2 {ys} MPa, Rm {uts} MPa", "EN 10088-2:2014 / ASTM A666 minimum values",
                          app, props, STEEL_SHADER, "9DA4AC", "ANSI32 (Steel)"))
write("Stainless 304 by supply condition.sldmat", "Stainless 304 by supply condition", steel)

# ------------------------------------------------------------------ structural steel S275 / S355 (EN 10025-2)
# EN 10025-2:2019 table 7, minimum upper yield strength ReH by nominal thickness, tensile strength Rm for 3-100 mm
# (thinner than 3 mm Rm is higher: 430 / 510 MPa; the lower figure is kept, on the safe side). Hot-finished (EN 10210)
# and cold-formed (EN 10219) hollow sections have the same values up to 40 mm. Physical properties per EN 1993-1-1
# 3.2.6 (E 210 GPa, nu 0.3, G 81 GPa, alpha 12e-6 /K, 7850 kg/m3) and EN 1993-1-2 at 20 C (conductivity 53 W/mK,
# specific heat 440 J/kgK).
CARBON_SHADER = ('      <shaders>\n'
                 '        <pwshader2 name="carbon steel" isNewShader="1" path="\\metal\\steel\\carbon steel.p2m" />\n'
                 '        <cgshader2 name="SteelPlainCarbon" />\n'
                 '      </shaders>\n')
CARBON = {"EX": 210e9, "NUXY": 0.30, "GXY": 81e9, "ALPX": 12.0e-6, "DENS": 7850, "KX": 53.0, "C": 440.0}
STRUCTURAL = [("S275", 410, [("t up to 16 mm", 275), ("t 16-40 mm", 265), ("t 40-63 mm", 255)]),
              ("S355", 470, [("t up to 16 mm", 355), ("t 16-40 mm", 345), ("t 40-63 mm", 335)])]
structural = []
for g, (grade, rm, bands) in enumerate(STRUCTURAL):
    for b, (band, reh) in enumerate(bands):
        name = f"{grade} (EN 10025-2, {band})"
        app = (f"Structural steel {grade} to EN 10025-2:2019 (plates, sections, bars; hollow sections to EN 10210 / "
               f"EN 10219 have the same values up to 40 mm), nominal thickness {band}: minimum yield ReH {reh} MPa, "
               f"minimum tensile strength Rm {rm} MPa (3-100 mm; under 3 mm Rm is higher, the lower is kept). "
               "Choose the band for the part's thickness: the yield steps down as the steel gets thicker.\n\n"
               "Physical properties: E 210 GPa, nu 0.30, G 81 GPa, density 7850 kg/m3, expansion 12e-6 /K "
               "(EN 1993-1-1 3.2.6); conductivity 53 W/mK, specific heat 440 J/kgK (EN 1993-1-2 at 20 C).")
        props = dict(CARBON, SIGYLD=reh * 1e6, SIGXT=rm * 1e6)
        structural.append(material(name, 9301 + 10 * g + b, f"min. ReH {reh} MPa, Rm {rm} MPa ({band})",
                                   "EN 10025-2:2019 minimum values; EN 1993-1-1", app, props, CARBON_SHADER, "8C8F94",
                                   "ANSI32 (Steel)"))
write("Structural steel EN 10025-2.sldmat", "Structural steel (EN 10025-2)", structural)
