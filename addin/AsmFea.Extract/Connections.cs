// The Connections tab's pure logic (D97, D98): which connections a SolidWorks selection names, the join a
// selection makes, and the colour view's record of what it changed (so every change is put back exactly).
// No SolidWorks here: the add-in turns the selection into SelItems and does the drawing behind ILookTarget.
using System;
using System.Collections.Generic;
using System.Linq;

namespace AsmFea.Extract
{
    /// <summary>One thing selected in SolidWorks, as the analysis names it: a part id (component, or
    /// component/body:name for a body of a multi-body part), and for a selected face its study face_ref.</summary>
    public class SelItem
    {
        public string PartId;
        public JObj FaceRef;            // {id, part, persistent_ref, fingerprint}; null for a component or body
        public SelItem(string partId, JObj faceRef = null) { PartId = partId; FaceRef = faceRef; }
    }

    public static class ConnMatch
    {
        /// <summary>The distinct parts of a selection, in the order selected.</summary>
        public static List<string> Parts(IEnumerable<SelItem> sel)
        {
            return sel.Where(s => s.PartId != null).Select(s => s.PartId).Distinct().ToList();
        }

        /// <summary>A part id, or a component that holds it (a multi-body part's component, a sub-assembly): the
        /// selection of a component names every part inside it.</summary>
        public static bool Covers(string selected, string partId)
        {
            return partId == selected || partId.StartsWith(selected + "/", StringComparison.Ordinal);
        }

        /// <summary>
        /// Which connections (connections.json records) the selection names, by index:
        ///   a selected face: the connections with that face as a side (its fingerprint, Rematch);
        ///   two or more parts: the connections between two of them;
        ///   one part: every connection of it.
        /// </summary>
        public static List<int> Match(IList<JObj> conns, IList<SelItem> sel)
        {
            var hits = new List<int>();
            var faces = sel.Where(s => s.FaceRef != null && s.FaceRef["fingerprint"] is JObj).ToList();
            var parts = Parts(sel);
            for (int i = 0; i < conns.Count; i++)
            {
                var c = conns[i];
                var cparts = ((c["parts"] as List<object>) ?? new List<object>()).OfType<string>().Take(2).ToList();
                if (cparts.Count < 2) continue;
                bool hit;
                if (faces.Count > 0)
                    hit = Sides(c).Any(sd => faces.Any(f => Covers(f.PartId, sd.Item1) && SameFace((JObj)f.FaceRef["fingerprint"], sd.Item2)));
                else if (parts.Count >= 2)
                    // one selected part at each end (two different selections: not two bodies of one component)
                    hit = parts.Any(a => parts.Any(b => a != b && Covers(a, cparts[0]) && Covers(b, cparts[1])));
                else if (parts.Count == 1)
                    hit = cparts.Any(p => Covers(parts[0], p));
                else hit = false;
                if (hit) hits.Add(i);
            }
            return hits;
        }

        /// <summary>A connection's sides: (part, face fingerprint), as resolve writes them.</summary>
        public static IEnumerable<Tuple<string, JObj>> Sides(JObj c)
        {
            foreach (var s in ((c["sides"] as List<object>) ?? new List<object>()).OfType<JObj>())
                if (s["fingerprint"] is JObj) yield return Tuple.Create((string)s["part"], (JObj)s["fingerprint"]);
        }

        public static bool SameFace(JObj a, JObj b)
        {
            return Rematch.Score(a, b) <= Rematch.MAX_SCORE * 0.2;      // the same face: far closer than a re-match
        }

        /// <summary>
        /// The join a selection makes (study joins entry), or null with the reason. Two parts: nothing else (the
        /// nearest faces), or a face on one and the other part (that face to the nearest), or a face on each.
        /// faceIds: the face_ref ids the selection's faces were stored under, by part.
        /// </summary>
        public static JObj JoinFrom(IList<SelItem> sel, string id, IDictionary<string, string> faceIds, out string problem)
        {
            problem = null;
            var parts = Parts(sel);
            if (parts.Count != 2)
            {
                problem = parts.Count < 2 ? "select two parts (or a face on each) to join"
                                          : "select only two parts (or a face on each) to join";
                return null;
            }
            var faces = new List<object>();
            foreach (var p in parts)
            {
                var onIt = sel.Where(s => s.PartId == p && s.FaceRef != null).ToList();
                if (onIt.Count > 1) { problem = "select at most one face on each part"; return null; }
                if (onIt.Count == 1)
                {
                    string fid;
                    if (!faceIds.TryGetValue(p, out fid)) { problem = "the face on " + p + " was not stored"; return null; }
                    faces.Add(fid);
                }
            }
            var j = new JObj { { "id", id }, { "parts", parts.Cast<object>().ToList() } };
            if (faces.Count > 0) j["faces"] = faces;
            return j;
        }
    }

    /// <summary>What the colour view does to the document: the add-in's SolidWorks calls (LIVE-T11/T12).</summary>
    public interface ILookTarget
    {
        /// <summary>The look (MaterialPropertyValues: r, g, b, ambient, diffuse, specular, shininess, transparency,
        /// emission) a component, or a body of a part document, shows now; null when it cannot be read.</summary>
        double[] GetLook(string key);
        void SetLook(string key, double[] look);
        /// <summary>Remove the look this view set at that level (the component's own appearance).</summary>
        void RemoveLook(string key);
        /// <summary>Colour faces (assembly context) in one appearance; returns a handle to take it off again, null when
        /// refused.</summary>
        object AddFaceColour(IList<object> faces, double[] look);
        void RemoveFaceColour(object handle);
    }

    /// <summary>
    /// The colour view's state (D98): the look of each component or body it touched as it was before, and the faces it
    /// coloured, so Clear puts back exactly what was there: the view's own appearance removed, and the look read
    /// again and set back only where removing it did not restore it (an appearance the user had set at that level).
    /// Clear is safe to call any number of times.
    /// </summary>
    public class ColourState
    {
        public const double TRANSPARENCY = 0.7;       // the other parts, seen through. Guess, for the live test
        public static readonly double[] RED = { 0.9, 0.05, 0.05 }, BLUE = { 0.15, 0.35, 0.95 };

        private readonly ILookTarget target;
        private readonly Dictionary<string, double[]> before = new Dictionary<string, double[]>();
        private readonly Dictionary<string, double[]> applied = new Dictionary<string, double[]>();   // set last: not set again
        private readonly List<object> faceHandles = new List<object>();
        public bool On { get { return before.Count > 0 || faceHandles.Count > 0; } }
        /// <summary>What was changed, for the journal (restore after a crash): key -> look before.</summary>
        public IDictionary<string, double[]> Before { get { return before; } }

        public ColourState(ILookTarget target) { this.target = target; }

        public static double[] Look(double[] rgb, double transparency, double[] like = null)
        {
            var l = like != null && like.Length >= 9 ? (double[])like.Clone() : new[] { 0.8, 0.8, 0.8, 0.5, 0.8, 0.5, 0.3, 0.0, 0.0 };
            if (rgb != null) { l[0] = rgb[0]; l[1] = rgb[1]; l[2] = rgb[2]; }
            l[7] = transparency;
            return l;
        }

        /// <summary>Make the components (or bodies) see-through, the rigid ones blue and opaque, and colour the faces.
        /// Each key's first look is kept; faces are coloured afresh each time.</summary>
        public void Apply(IEnumerable<string> seeThrough, IEnumerable<string> rigid, IEnumerable<object> redFaces,
                          IEnumerable<object> blueFaces = null)
        {
            ApplyLooks(seeThrough, rigid);
            ApplyFaces(redFaces, blueFaces);
        }

        /// <summary>The components (or bodies): see-through, the rigid ones blue and opaque.</summary>
        public void ApplyLooks(IEnumerable<string> seeThrough, IEnumerable<string> rigid)
        {
            var rig = new HashSet<string>(rigid ?? new string[0]);
            var keep = new HashSet<string>((seeThrough ?? new string[0]).Concat(rig));
            // what an earlier call changed that is no longer wanted (the parts of the connection now shown): as it was
            foreach (var k in applied.Keys.Where(k => !keep.Contains(k)).ToList())
            {
                Restore(k);
                applied.Remove(k);
            }
            foreach (var k in (seeThrough ?? new string[0]).Concat(rig).Distinct())
            {
                if (!before.ContainsKey(k)) before[k] = target.GetLook(k);
                var was = before[k];
                var want = rig.Contains(k) ? Look(BLUE, 0.0, was) : Look(null, TRANSPARENCY, was);
                double[] cur;
                if (applied.TryGetValue(k, out cur) && SameLook(cur, want)) continue;     // a big assembly: only what changes
                target.SetLook(k, want);
                applied[k] = want;
            }
        }

        /// <summary>The faces, red (connections) and blue (rigid bodies of multi-body parts), afresh each time: one
        /// appearance per colour.</summary>
        public void ApplyFaces(IEnumerable<object> redFaces, IEnumerable<object> blueFaces = null)
        {
            ClearFaces();
            foreach (var pair in new[] { Tuple.Create(redFaces, RED), Tuple.Create(blueFaces, BLUE) })
            {
                var list = (pair.Item1 ?? new object[0]).ToList();
                if (list.Count == 0) continue;
                var h = target.AddFaceColour(list, Look(pair.Item2, 0.0));
                if (h != null) faceHandles.Add(h);
            }
        }

        public void ClearFaces()
        {
            foreach (var h in faceHandles)
                try { target.RemoveFaceColour(h); } catch { }
            faceHandles.Clear();
        }

        /// <summary>Everything back as it was.</summary>
        public void Clear()
        {
            ClearFaces();
            foreach (var k in before.Keys.ToList()) Restore(k);
            before.Clear();
            applied.Clear();
        }

        /// <summary>One key's look as it was: the view's own removed, the old one set back where that did not restore it.</summary>
        private void Restore(string k)
        {
            double[] was;
            if (!before.TryGetValue(k, out was)) return;
            try
            {
                target.RemoveLook(k);
                if (was != null && !SameLook(target.GetLook(k), was)) target.SetLook(k, was);
            }
            catch { }
        }

        public static bool SameLook(double[] a, double[] b)
        {
            if (a == null || b == null) return a == b;
            if (a.Length != b.Length) return false;
            for (int i = 0; i < a.Length; i++)
                if (Math.Abs(a[i] - b[i]) > 1e-6) return false;
            return true;
        }
    }
}
