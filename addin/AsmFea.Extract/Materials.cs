// Material properties from SolidWorks material libraries (.sldmat: XML, classification >
// material > physicalproperties, SI units). A material with no modulus, Poisson's ratio,
// density or yield is not exported: the pipeline then raises MATERIAL_MISSING for its parts,
// because a missing property is an exception, never a default (PLAN.md "Extraction").
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Xml;

namespace AsmFea.Extract
{
    public class MaterialProps
    {
        public string Name;
        public string Library;
        public double? E, Nu, Density, Yield, Ultimate;   // MPa, -, tonne/mm^3, MPa, MPa

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
