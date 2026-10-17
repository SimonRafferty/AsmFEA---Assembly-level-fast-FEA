// The Fatigue tab's data (release 2 F5, D108): study.fatigue as the pane edits it, with no UI and no SolidWorks, so it
// is tested in AsmFea.Tests. The pipeline's schema is the definition (schema/model.schema.json, study.fatigue); the
// lists of choices mirror asmfea/fatigue/tables.py.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;

namespace AsmFea.Extract
{
    public static class FatigueSettings
    {
        /// <summary>(key, what the drop-down shows): the standards with a copy or cross-checked data (D102).</summary>
        public static readonly string[][] Standards =
        {
            new[] { "bs7608", "BS 7608 (UK, default)" },
            new[] { "pd5500", "PD 5500 Annex C" },
            new[] { "en13001_3_1", "EN 13001-3-1 (cranes)" },
            new[] { "en1993_1_9", "EN 1993-1-9:2005" },
        };

        public static readonly string[] MeanStress = { "goodman", "gerber", "none" };

        public static string[] StructuralChoices(string standard)
        {
            return standard == "bs7608" || standard == "pd5500" ? new[] { "C", "D", "E", "F" } : new[] { "112", "100", "90" };
        }

        public static string StructuralDefault(string standard)
        {
            return standard == "bs7608" || standard == "pd5500" ? "D" : "90";
        }

        public static string[] ParentClasses(string standard)
        {
            switch (standard)
            {
                case "bs7608": return new[] { "B", "C" };
                case "pd5500": return new[] { "C" };
                case "en1993_1_9": return new[] { "160", "140", "125" };
                default: return new[] { "D.1", "D.1-A1" };
            }
        }

        /// <summary>study.fatigue of the study's settings, made when absent (with one empty row and BS 7608).</summary>
        public static JObj Get(JObj studySettings, bool create)
        {
            var f = studySettings["fatigue"] as JObj;
            if (f == null && create)
            {
                f = new JObj { { "standard", "bs7608" }, { "spectrum", new List<object>() } };
                studySettings["fatigue"] = f;
            }
            return f;
        }

        public static void Remove(JObj studySettings) { studySettings.Remove("fatigue"); }

        static double? Num(object v)
        {
            if (v == null) return null;
            if (v is double d) return d;
            double x;
            return double.TryParse(Convert.ToString(v, CultureInfo.InvariantCulture), NumberStyles.Float, CultureInfo.InvariantCulture, out x)
                ? x : (double?)null;
        }

        /// <summary>A spectrum row from the grid's cells; null and why when a cell is not usable. minCase "" or null:
        /// the same case.</summary>
        public static JObj Row(string loadCase, string maxPct, string minCase, string minPct, string cycles, out string why)
        {
            why = null;
            var mx = Num(maxPct);
            var mn = Num(minPct);
            var n = Num(cycles);
            if (string.IsNullOrWhiteSpace(loadCase)) { why = "choose a load case"; return null; }
            if (mx == null) { why = "the maximum is not a number"; return null; }
            if (mn == null) { why = "the minimum is not a number"; return null; }
            if (n == null || n <= 0) { why = "cycles must be a positive number"; return null; }
            var r = new JObj { { "case", loadCase }, { "max_pct", mx.Value }, { "min_pct", mn.Value }, { "cycles", n.Value } };
            if (!string.IsNullOrWhiteSpace(minCase) && minCase != loadCase) r["min_case"] = minCase;
            return r;
        }

        /// <summary>What stops the fatigue settings being used, in words (empty when none): rows naming unknown load
        /// cases, no rows, a material with no data.</summary>
        public static List<string> Problems(JObj fatigue, ICollection<string> loadCases, ICollection<string> materials)
        {
            var out_ = new List<string>();
            if (fatigue == null) return out_;
            var rows = (fatigue["spectrum"] as List<object>)?.OfType<JObj>().ToList() ?? new List<JObj>();
            if (rows.Count == 0) out_.Add("The spectrum has no rows.");
            int i = 0;
            foreach (var r in rows)
            {
                i++;
                foreach (var c in new[] { (string)r["case"], (string)r["min_case"] ?? (string)r["case"] }.Distinct())
                    if (c != null && !loadCases.Contains(c)) out_.Add("Row " + i + ": load case " + c + " is not one of the study's.");
            }
            // a material with no S-N entry uses the SolidWorks material's curve or AsmFEA's built-in data (D111); the
            // pipeline reports one with none at all (FATIGUE_NO_SN_CURVE)
            return out_;
        }

        /// <summary>The study folder's study.json text with only its fatigue settings replaced (removed when null).
        /// Assess fatigue writes this instead of exporting the model again: a fatigue change needs nothing from
        /// SolidWorks, and a fresh export after a save changed the faces' persistent references, which made the static
        /// results look out of date (D115).</summary>
        public static string WithFatigue(string studyJson, JObj fatigue)
        {
            var doc = Json.Parse(studyJson) as JObj;
            if (doc == null) throw new FormatException("study.json is not a JSON object");
            var s = doc["study"] as JObj;
            if (s == null) doc["study"] = s = new JObj();
            if (fatigue == null) s.Remove("fatigue"); else s["fatigue"] = fatigue;
            return Json.Write(doc);
        }

        /// <summary>One bonded joint's figures from results/fatigue.json for the joints list.</summary>
        public class JointFigures
        {
            public string Id;
            /// <summary>The weld's (member side's) and the wall's fatigue factors: NaN not assessed, +Infinity no damage.</summary>
            public double Weld = double.NaN, Wall = double.NaN;
            public bool WallIndicative;
            public double Lowest { get { return Math.Min(double.IsNaN(Weld) ? double.PositiveInfinity : Weld, double.IsNaN(Wall) ? double.PositiveInfinity : Wall); } }
            public bool Assessed { get { return !double.IsNaN(Weld) || !double.IsNaN(Wall); } }
        }

        static double Factor(JObj rec)
        {
            if (rec == null) return double.NaN;
            var k = rec["factor_k"];                              // null in JSON: no damage at any load
            return k == null ? double.PositiveInfinity : Convert.ToDouble(k, CultureInfo.InvariantCulture);
        }

        /// <summary>The joints in increasing order of their lowest fatigue factor (weld or wall), so those that fail come
        /// first (the user, 2026-10-17); joints not assessed after them in their own order.</summary>
        public static List<JointFigures> JointOrder(IEnumerable<string> ids, JObj fatigueResults)
        {
            var welds = fatigueResults?["joints"] as JObj;
            var walls = fatigueResults?["walls"] as JObj;
            var list = ids.Select((id, i) =>
            {
                var wall = walls?[id] as JObj;
                return new { i, f = new JointFigures { Id = id, Weld = Factor(welds?[id] as JObj), Wall = Factor(wall),
                                                      WallIndicative = ((string)wall?["method"] ?? "").Contains("indicative") } };
            }).ToList();
            return list.OrderBy(x => x.f.Assessed ? 0 : 1).ThenBy(x => x.f.Assessed ? x.f.Lowest : 0).ThenBy(x => x.i)
                       .Select(x => x.f).ToList();
        }

        /// <summary>A factor as the joints list shows it.</summary>
        public static string FactorText(double k)
        {
            return double.IsNaN(k) ? "" : double.IsPositiveInfinity(k) ? "no damage" : k.ToString("0.##", CultureInfo.InvariantCulture);
        }

        /// <summary>The surface finishes for AsmFEA's built-in data (Marin's factor for steel; D111).</summary>
        public static readonly string[] Surfaces = { "", "polished", "ground", "machined", "hot_rolled", "as_forged" };

        /// <summary>Set a material's S-N data: AsmFEA's built-in data ("builtin", only a surface finish stored, or nothing),
        /// a Basquin fit of the user's data (Sf, b) or a class of the standard.</summary>
        public static void SetMaterial(JObj fatigue, string material, string kind, double? sf, double? b, string cls, string source,
                                       string surface = null)
        {
            var list = fatigue["materials"] as List<object>;
            if (list == null) fatigue["materials"] = list = new List<object>();
            list.RemoveAll(x => x is JObj j && (string)j["material"] == material);
            var e = new JObj { { "material", material } };
            if (kind == "builtin")
            {
                if (string.IsNullOrWhiteSpace(surface)) return;   // nothing to store: the defaults
                e["surface"] = surface;
                list.Add(e);
                return;
            }
            if (kind == "class" && !string.IsNullOrWhiteSpace(cls)) e["standard_class"] = cls;
            else if (kind == "basquin" && sf > 0 && b < 0) e["basquin"] = new JObj { { "Sf", sf.Value }, { "b", b.Value } };
            else return;                                          // incomplete: nothing stored for it
            if (!string.IsNullOrWhiteSpace(source)) e["source"] = source;
            list.Add(e);
        }

        /// <summary>The pane's key figures from results/fatigue.json: the governing item, its damage and factor, and the
        /// result; then the not-assessed items.</summary>
        public static string KeyFigures(JObj fatigueResults)
        {
            if (fatigueResults == null) return "No fatigue results yet: set the spectrum and S-N data, then Assess fatigue.";
            var g = fatigueResults["governing"] as JObj;
            if (g == null) return "Nothing was assessed for fatigue.";
            double d = g["damage"] == null ? 0 : Convert.ToDouble(g["damage"], CultureInfo.InvariantCulture);
            bool pass = g["pass"] is bool p && p;
            // the factor is null (JSON has no infinity) when nothing does any damage
            string k = g["factor_k"] == null ? "no damage at any load" :
                       Convert.ToDouble(g["factor_k"], CultureInfo.InvariantCulture).ToString("0.##", CultureInfo.InvariantCulture) + " on stress";
            // a small damage in E notation: "0" read as no damage at all
            string dt = d > 0 && d < 0.001 ? d.ToString("0.##E+0", CultureInfo.InvariantCulture) : d.ToString("0.###", CultureInfo.InvariantCulture);
            var s = (pass ? "PASS" : "FAIL") + "  fatigue damage over the design life " + dt
                    + " (limit 1); fatigue factor " + k + ", at " + (string)g["item"] + ".";
            var warn = (fatigueResults["warnings"] as List<object>)?.OfType<JObj>()
                       .Count(w => (string)w["code"] == "FATIGUE_RECEIVING_SIDE_NOT_ASSESSED") ?? 0;
            if (warn > 0) s += "  " + warn + " joint wall(s) not assessed: check by hand.";
            var ind = (fatigueResults["warnings"] as List<object>)?.OfType<JObj>()
                      .Count(w => (string)w["code"] == "FATIGUE_RECEIVING_SIDE_INDICATIVE") ?? 0;
            if (ind > 0) s += "  " + ind + " joint wall(s) indicative only (read from the model).";
            return s;
        }
    }
}
