// Material properties from SolidWorks material libraries (.sldmat: XML, classification >
// material > physicalproperties, SI units). A material with no modulus, Poisson's ratio,
// density or yield is not exported: the pipeline then raises MATERIAL_MISSING for its parts,
// because a missing property is an exception, never a default (PLAN.md "Extraction").
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Xml;

namespace AsmFea.Extract
{
    public class MaterialProps
    {
        public string Name;
        public string Library;
        public double? E, Nu, Density, Yield, Ultimate;   // MPa, -, tonne/mm^3, MPa, MPa
        /// <summary>S-N curves stored with the material (its Fatigue SN Curves tab; materialcurve type 19): the stress
        /// ratio and [cycles, stress amplitude MPa] points (D109).</summary>
        public readonly List<Tuple<double, List<double[]>>> SnCurves = new List<Tuple<double, List<double[]>>>();

        public bool Complete { get { return E.HasValue && Nu.HasValue && Density.HasValue && Yield.HasValue; } }

        public IEnumerable<string> MissingProperties()
        {
            if (!E.HasValue) yield return "E (EX)";
            if (!Nu.HasValue) yield return "Poisson's ratio (NUXY)";
            if (!Density.HasValue) yield return "density (DENS)";
            if (!Yield.HasValue) yield return "yield (SIGYLD)";
        }

        public JObj ToJson()
        {
            var o = new JObj { { "name", Name }, { "E", E.Value }, { "nu", Nu.Value }, { "density", Density.Value }, { "yield", Yield.Value } };
            if (Ultimate.HasValue) o["ultimate"] = Ultimate.Value;
            if (SnCurves.Count > 0)
                o["sn_curves"] = SnCurves.Select(c => (object)new JObj {
                    { "r_ratio", c.Item1 }, { "points", c.Item2.Select(p => (object)new List<object> { p[0], p[1] }).ToList() },
                    { "source", "SolidWorks material library " + Library } }).ToList();
            return o;
        }
    }

    public class MaterialLibrary
    {
        // library name (file name without extension, lower case) -> material name -> props
        private readonly Dictionary<string, Dictionary<string, MaterialProps>> libs =
            new Dictionary<string, Dictionary<string, MaterialProps>>(StringComparer.OrdinalIgnoreCase);

        public void LoadFile(string path)
        {
            string lib = Path.GetFileNameWithoutExtension(path);
            var doc = new XmlDocument();
            doc.Load(path);                   // the reader honours the UTF-16 byte-order mark
            LoadXml(doc, lib);
        }

        public void LoadXml(XmlDocument doc, string lib)
        {
            Dictionary<string, MaterialProps> mats;
            if (!libs.TryGetValue(lib, out mats)) libs[lib] = mats = new Dictionary<string, MaterialProps>(StringComparer.OrdinalIgnoreCase);
            foreach (XmlNode m in doc.GetElementsByTagName("material"))
            {
                var name = m.Attributes?["name"]?.Value;
                if (string.IsNullOrEmpty(name)) continue;
                var p = new MaterialProps { Name = name, Library = lib };
                foreach (XmlNode c in m.ChildNodes)
                {
                    if (c.LocalName != "physicalproperties") continue;
                    foreach (XmlNode q in c.ChildNodes)
                    {
                        if (q.LocalName == "materialcurve" && q.Attributes?["type"]?.Value == "19")
                        {
                            // an S-N curve: <data points="N S_Pa"/> rows, then <data extraparam="R"/> (the stress ratio;
                            // -1 fully reversed, as the library's own (SN) materials). Stress amplitude (D109; LIVE-T21)
                            var pts = new List<double[]>();
                            double r = -1;
                            foreach (XmlNode d in q.ChildNodes)
                            {
                                var ps = d.Attributes?["points"]?.Value;
                                var ex = d.Attributes?["extraparam"]?.Value;
                                double rr;
                                if (ex != null && double.TryParse(ex, NumberStyles.Float, CultureInfo.InvariantCulture, out rr)) r = rr;
                                if (ps == null) continue;
                                var parts = ps.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
                                double n, sv;
                                if (parts.Length >= 2 && double.TryParse(parts[0], NumberStyles.Float, CultureInfo.InvariantCulture, out n)
                                    && double.TryParse(parts[1], NumberStyles.Float, CultureInfo.InvariantCulture, out sv) && n > 0 && sv > 0)
                                    pts.Add(new[] { n, sv * Units.PA_TO_MPA });
                            }
                            if (pts.Count >= 2) p.SnCurves.Add(Tuple.Create(r, pts));
                            continue;
                        }
                        double v;
                        var s = q.Attributes?["value"]?.Value;
                        if (s == null || !double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out v)) continue;
                        switch (q.LocalName)
                        {
                            case "EX": p.E = v * Units.PA_TO_MPA; break;
                            case "NUXY": p.Nu = v; break;
                            case "DENS": p.Density = v * Units.KG_M3_TO_T_MM3; break;
                            case "SIGYLD": p.Yield = v * Units.PA_TO_MPA; break;
                            case "SIGXT": p.Ultimate = v * Units.PA_TO_MPA; break;
                        }
                    }
                }
                // zero means "not set" in the SolidWorks libraries
                if (p.E <= 0) p.E = null;
                if (p.Density <= 0) p.Density = null;
                if (p.Yield <= 0) p.Yield = null;
                if (p.Ultimate <= 0) p.Ultimate = null;
                if (p.Nu.HasValue && (p.Nu < 0 || p.Nu >= 0.5)) p.Nu = null;
                mats[name] = p;
            }
        }

        /// <summary>
        /// Find a material by name. The database SolidWorks names for the part is searched first
        /// (it may be a full path or a library name), then every loaded library. Null if absent.
        /// </summary>
        public MaterialProps Find(string name, string database)
        {
            if (string.IsNullOrEmpty(name)) return null;
            MaterialProps p;
            Dictionary<string, MaterialProps> mats;
            if (!string.IsNullOrEmpty(database))
            {
                string lib = Path.GetFileNameWithoutExtension(database);
                if (libs.TryGetValue(lib, out mats) && mats.TryGetValue(name, out p)) return p;
            }
            foreach (var l in libs.Values)
                if (l.TryGetValue(name, out p)) return p;
            return null;
        }
    }
}
