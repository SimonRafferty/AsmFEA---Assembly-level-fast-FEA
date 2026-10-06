// Structural member data that needs no SolidWorks: profile parsing and clean spans.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;

namespace AsmFea.Extract
{
    /// <summary>Every numeric default of the add-in, with its source (the Python side keeps its own in defaults.py).</summary>
    public static class AddinDefaults
    {
        /// <summary>Span ends are pulled back from joints by this many section depths (PLAN.md "Clean spans").</summary>
        public const double SPAN_MARGIN_DEPTHS = 1.5;
        /// <summary>A span shorter than this many section depths is dropped; a member with none is tet-meshed. Guess.</summary>
        public const double SPAN_MIN_DEPTHS = 2.0;
        /// <summary>Tessellation chord tolerance for face centroids, m. Python matches centroids to 0.05 mm,
        /// and the chord error moves a curved face's centroid by about 2/3 of the tolerance.</summary>
        public const double TESS_CHORD_TOL_M = 1e-6;
        /// <summary>Tessellation angle tolerance, radians (about 1 degree).</summary>
        public const double TESS_ANGLE_TOL = 0.0175;
        /// <summary>A face counts as parallel to the member axis when |cos| of its normal to the axis is below this.</summary>
        public const double AXIS_PARALLEL_COS = 1e-4;
    }

    public class Span
    {
        public double S0, S1;
        public Span(double s0, double s1) { S0 = s0; S1 = s1; }
        public double Length { get { return S1 - S0; } }
        public override string ToString() { return string.Format(CultureInfo.InvariantCulture, "[{0}, {1}]", S0, S1); }
    }

    public static class CleanSpans
    {
        /// <summary>
        /// Clean spans of a member along its path, 0..length (mm): the length not touched by end
        /// trims, holes, cuts or copes, each end pulled back by the margin, spans below the
        /// minimum dropped. `touched` are the intervals (along the path) covered by any face that
        /// is not part of the plain extruded profile.
        /// </summary>
        public static List<Span> Compute(double length, IEnumerable<Span> touched, double depth,
                                         double marginDepths = AddinDefaults.SPAN_MARGIN_DEPTHS,
                                         double minDepths = AddinDefaults.SPAN_MIN_DEPTHS)
        {
            if (length <= 0) throw new ArgumentException("member length must be positive");
            if (depth <= 0) throw new ArgumentException("section depth must be positive");
            // merge the touched intervals, clipped to the member
            var cuts = touched.Select(t => new Span(Math.Max(0, Math.Min(t.S0, t.S1)), Math.Min(length, Math.Max(t.S0, t.S1))))
                              .Where(t => t.S1 >= t.S0)
                              .OrderBy(t => t.S0).ToList();
            var merged = new List<Span>();
            foreach (var c in cuts)
            {
                if (merged.Count > 0 && c.S0 <= merged[merged.Count - 1].S1)
                    merged[merged.Count - 1].S1 = Math.Max(merged[merged.Count - 1].S1, c.S1);
                else
                    merged.Add(new Span(c.S0, c.S1));
            }
            // the gaps between them are untouched; the member ends always count as joints
            var free = new List<Span>();
            double at = 0;
            foreach (var m in merged)
            {
                if (m.S0 > at) free.Add(new Span(at, m.S0));
                at = Math.Max(at, m.S1);
            }
            if (at < length) free.Add(new Span(at, length));
            double margin = marginDepths * depth, minLen = minDepths * depth;
            var spans = new List<Span>();
            foreach (var f in free)
            {
                var s = new Span(f.S0 + margin, f.S1 - margin);
                if (s.Length >= minLen) spans.Add(s);
            }
            return spans;
        }
    }

    /// <summary>
    /// Profile type and dimensions from a weldment profile. Two library layouts exist:
    /// one file per size, ...\iso\square tube\100 x 100 x 5.sldlfp (the folder names the shape,
    /// the file the size), and a configured profile, ...\iso\square tube.sldlfp with the size as
    /// the feature's configuration (SolidWorks 2026 ships this). Anything not recognised becomes
    /// type "other" with no dimensions, which the pipeline then tet-meshes.
    /// </summary>
    public static class Profiles
    {
        public static JObj Parse(string profilePath, string configuration = null)
        {
            var o = new JObj();
            if (string.IsNullOrEmpty(profilePath)) { o["type"] = "other"; return o; }
            string folder = (Path.GetFileName(Path.GetDirectoryName(profilePath)) ?? "").ToLowerInvariant();
            string size = Path.GetFileNameWithoutExtension(profilePath) ?? "";
            if (!Regex.IsMatch(size, @"\d") && !string.IsNullOrEmpty(configuration))
            {
                folder = size.ToLowerInvariant();       // configured profile: the file is the shape
                size = configuration;
                o["configuration"] = configuration;
            }
            o["source"] = profilePath;
            var nums = Regex.Matches(size, @"\d+(?:\.\d+)?").Cast<Match>()
                            .Select(m => double.Parse(m.Value, CultureInfo.InvariantCulture)).ToArray();
            string type = Classify(folder, nums.Length);
            o["type"] = type;
            switch (type)
            {
                case "SHS":
                    if (nums.Length >= 3) { o["b"] = nums[0]; o["h"] = nums[1]; o["t"] = nums[2]; }
                    else if (nums.Length == 2) { o["b"] = nums[0]; o["h"] = nums[0]; o["t"] = nums[1]; }
                    break;
                case "RHS":
                case "angle":
                case "channel":
                    if (nums.Length >= 3) { o["b"] = nums[0]; o["h"] = nums[1]; o["t"] = nums[2]; }
                    break;
                case "CHS":
                    if (nums.Length >= 2) { o["d"] = nums[0]; o["t"] = nums[1]; }
                    break;
                case "flat":
                    if (nums.Length >= 2) { o["b"] = nums[0]; o["t"] = nums[1]; }
                    break;
                case "round":
                    if (nums.Length >= 1) o["d"] = nums[0];
                    break;
            }
            return o;
        }

        private static string Classify(string folder, int n)
        {
            if (folder.Contains("square tube") || folder.Contains("shs")) return "SHS";
            if (folder.Contains("rectangular tube") || folder.Contains("rhs")) return "RHS";
            if (folder.Contains("pipe") || folder.Contains("chs") || folder.Contains("round tube")) return "CHS";
            if (folder.Contains("angle")) return "angle";
            if (folder.Contains("channel") || folder.Contains("c channel")) return "channel";
            if (folder.Contains("beam") || folder.Contains("i-") || folder.Contains("ipe") || folder.Contains("hea")) return "I";
            if (folder.Contains("flat") || folder.Contains("bar")) return n >= 2 ? "flat" : "round";
            if (folder.Contains("round")) return "round";
            return "other";
        }

        /// <summary>Section depth used for the span margin: the largest outer dimension known.</summary>
        public static double Depth(JObj profile, double fallback)
        {
            double d = 0;
            foreach (var k in new[] { "b", "h", "d" })
                if (profile.Has(k)) d = Math.Max(d, Convert.ToDouble(profile[k]));
            return d > 0 ? d : fallback;
        }
    }
}
