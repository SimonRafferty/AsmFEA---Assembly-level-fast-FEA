// Whether a study's stored results are current: the Run tab's bar opens green when they match the study and model
// as they are now, red when either changed since the run that made them (the user, 2026-10-16). No SolidWorks here:
// the model's state comes in as a signature string worked out by the add-in.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;

namespace AsmFea.Extract
{
    public static class ResultsState
    {
        /// <summary>Written by the add-in into run/&lt;job&gt;/ as a job starts: the study and model signature it exported.</summary>
        public const string InputsFile = "addin_inputs.json";

        /// <summary>The record of what a job was started from.</summary>
        public static JObj Inputs(JObj study, string signature)
        {
            return new JObj { { "schema_version", "1.0" }, { "study", study }, { "signature", signature } };
        }

        /// <summary>A short digest of the model's state lines (order matters; the caller sorts them).</summary>
        public static string Signature(IEnumerable<string> lines)
        {
            using (var sha = SHA1.Create())
            {
                var bytes = sha.ComputeHash(Encoding.UTF8.GetBytes(string.Join("\n", lines)));
                return string.Concat(bytes.Select(b => b.ToString("x2", CultureInfo.InvariantCulture)));
            }
        }

        /// <summary>The newest run folder whose job finished with a solve (a reused solve counts: its results are
        /// the ones for that job's inputs); null when there is none.</summary>
        public static string LastSolvedRun(string studyFolder)
        {
            string runs = Path.Combine(studyFolder, "run");
            if (!Directory.Exists(runs)) return null;
            foreach (var dir in Directory.GetDirectories(runs).OrderByDescending(d => Path.GetFileName(d), StringComparer.Ordinal))
            {
                try
                {
                    var job = (JObj)Json.Parse(File.ReadAllText(Path.Combine(dir, "job.json"), Encoding.UTF8));
                    var stages = (job["stages"] as List<object>) ?? new List<object>();
                    if ((string)job["state"] == "done" && stages.OfType<string>().Contains("solve")) return dir;
                }
                catch { }                                       // no job.json, or mid-write: not a finished run
            }
            return null;
        }

        /// <summary>
        /// True when the last solved run's inputs match <paramref name="study"/> and <paramref name="signature"/>
        /// (the pass/fail limits aside: changing them re-checks the results, no solve). A run made before the add-in kept
        /// its inputs is compared with study.json when no later job rewrote it, its model taken as unchanged.
        /// </summary>
        public static bool Current(string studyFolder, JObj study, string signature, out string why)
        {
            string run = LastSolvedRun(studyFolder);
            if (run == null) { why = "no results yet"; return false; }
            JObj stored = null;
            string storedSig = null;
            string inputs = Path.Combine(run, InputsFile);
            try
            {
                if (File.Exists(inputs))
                {
                    var rec = (JObj)Json.Parse(File.ReadAllText(inputs, Encoding.UTF8));
                    stored = rec["study"] as JObj;
                    storedSig = rec["signature"] as string;
                }
                else
                {
                    // study.json is rewritten by every export: it is this run's only when no job started after it
                    string newest = Directory.GetDirectories(Path.GetDirectoryName(run))
                                             .Select(Path.GetFileName).OrderByDescending(n => n, StringComparer.Ordinal).First();
                    string sj = Path.Combine(studyFolder, "study.json");
                    if (newest == Path.GetFileName(run) && File.Exists(sj)) stored = (JObj)Json.Parse(File.ReadAllText(sj, Encoding.UTF8));
                }
            }
            catch (Exception e) { why = "the last run's inputs could not be read (" + e.Message + ")"; return false; }
            if (stored == null) { why = "the last run's inputs are not known"; return false; }
            if (!SameStudy(stored, study)) { why = "the study changed since the last run"; return false; }
            if (storedSig != null && storedSig != signature) { why = "the model changed since the last run"; return false; }
            why = null;
            return true;
        }

        /// <summary>The two studies set up the same analysis: equal apart from the pass/fail limits, the fatigue settings (no
        /// static input, D105), key order, and empty lists or objects against none (the Connections tab adds empty hinges,
        /// joins and overrides lists as it reads a study: the Run bar went red on opening it, D124).</summary>
        public static bool SameStudy(JObj a, JObj b)
        {
            if (a == null || b == null) return a == b;
            return Same(WithoutLimits(a), WithoutLimits(b));
        }

        private static JObj WithoutLimits(JObj st)
        {
            var copy = (JObj)Json.Parse(Json.Write(st));
            var s = copy["study"] as JObj;
            if (s != null) { s.Remove("limits"); s.Remove("fatigue"); }
            return copy;
        }

        private static bool Same(object x, object y)
        {
            if (x is JObj && y is JObj)
            {
                var a = ((JObj)x).Where(kv => !Empty(kv.Value)).ToDictionary(kv => kv.Key, kv => kv.Value);
                var b = ((JObj)y).Where(kv => !Empty(kv.Value)).ToDictionary(kv => kv.Key, kv => kv.Value);
                return a.Count == b.Count && a.All(kv => b.ContainsKey(kv.Key) && Same(kv.Value, b[kv.Key]));
            }
            var lx = x as List<object>;
            var ly = y as List<object>;
            if (lx != null && ly != null) return lx.Count == ly.Count && lx.Zip(ly, Same).All(t => t);
            if (IsNumber(x) && IsNumber(y)) return Convert.ToDouble(x, CultureInfo.InvariantCulture) == Convert.ToDouble(y, CultureInfo.InvariantCulture);
            return Equals(x, y);
        }

        /// <summary>No value: null, an empty list or an empty object.</summary>
        private static bool Empty(object v)
        {
            return v == null || (v is List<object> l && l.Count == 0) || (v is JObj o && !o.Any());
        }

        private static bool IsNumber(object v) { return v is double || v is long || v is int || v is float || v is decimal; }
    }
}
