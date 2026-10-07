// The studies of one assembly, held in memory and persisted inside the assembly document
// (PLAN.md "Persistence"). Each study is a study.json document (schema/model.schema.json:
// face_refs, loads, fixtures, overrides, study). Pure data: the add-in reads and writes the
// document stream.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;

namespace AsmFea.Extract
{
    public class StudyStore
    {
        public const string FORMAT = "asmfea-studies-1";
        private readonly SortedDictionary<string, JObj> studies = new SortedDictionary<string, JObj>(StringComparer.OrdinalIgnoreCase);

        /// <summary>True once the store differs from what the document holds (until the next save).</summary>
        public bool Changed { get; set; }

        public IEnumerable<string> Names { get { return studies.Keys; } }

        public JObj Get(string name)
        {
            JObj s;
            return studies.TryGetValue(name, out s) ? s : null;
        }

        /// <summary>The study, created with defaults when missing.</summary>
        public JObj GetOrCreate(string name)
        {
            var s = Get(name);
            if (s != null) return s;
            s = NewStudy(name);
            studies[name] = s;
            Changed = true;
            return s;
        }

        public void Set(string name, JObj study)
        {
            studies[name] = study;
            Changed = true;
        }

        public bool Remove(string name)
        {
            bool r = studies.Remove(name);
            Changed |= r;
            return r;
        }

        public static JObj NewStudy(string name)
        {
            return new JObj
            {
                { "schema_version", "1.0" },
                { "face_refs", new List<object>() },
                { "loads", new List<object>() },
                { "fixtures", new List<object>() },
                { "overrides", new List<object>() },
                { "study", new JObj { { "name", name }, { "analyses", new List<object> { "static" } },
                                     { "load_cases", new List<object> { "LC1" } },
                                     { "mesh", new JObj { { "fidelity", "standard" }, { "convergence_check", true } } } } },
            };
        }

        public static List<object> List(JObj study, string key)
        {
            var l = study[key] as List<object>;
            if (l == null) study[key] = l = new List<object>();
            return l;
        }

        /// <summary>A new id with this prefix not used by any item of the study (L1, LD2, FX3 ...).</summary>
        public static string NextId(JObj study, string key, string prefix)
        {
            var used = new HashSet<string>(List(study, key).OfType<JObj>().Select(o => (string)o["id"]));
            for (int i = 1; ; i++)
                if (!used.Contains(prefix + i.ToString(CultureInfo.InvariantCulture))) return prefix + i.ToString(CultureInfo.InvariantCulture);
        }

        /// <summary>Face refs no load, fixture or hinge uses any more are dropped.</summary>
        public static void Prune(JObj study)
        {
            var used = new HashSet<string>();
            foreach (var key in new[] { "loads", "fixtures", "hinges" })
                foreach (JObj item in List(study, key).OfType<JObj>())
                    foreach (var f in (item["faces"] as List<object>) ?? new List<object>())
                        used.Add((string)f);
            List(study, "face_refs").RemoveAll(o => !used.Contains((string)((JObj)o)["id"]));
        }

        // ------------------------------------------------------------------ serialisation

        public string ToJson()
        {
            var all = new JObj();
            foreach (var kv in studies) all[kv.Key] = kv.Value;
            return Json.Write(new JObj { { "format", FORMAT }, { "studies", all } });
        }

        public static StudyStore FromJson(string text)
        {
            var store = new StudyStore();
            if (string.IsNullOrWhiteSpace(text)) return store;
            var doc = Json.Parse(text) as JObj;
            if (doc == null || (string)doc["format"] != FORMAT)
                throw new FormatException("not an asmfea study store (format " + (doc == null ? "?" : doc["format"]) + ")");
            var all = doc["studies"] as JObj;
            if (all != null)
                foreach (var kv in all)
                    if (kv.Value is JObj) store.studies[kv.Key] = (JObj)kv.Value;
            return store;
        }
    }
}
