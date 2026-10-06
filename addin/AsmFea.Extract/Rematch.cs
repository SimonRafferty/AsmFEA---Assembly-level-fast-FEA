// Reference fallback (PLAN.md "Persistence and reference fallback"): when a face's persistent
// reference no longer resolves, look for the face again by its fingerprint among the faces of
// the same part. One clear match is accepted and logged; no match or several close ones is
// an exception. Pure logic: the SolidWorks side supplies fingerprints.
using System;
using System.Collections.Generic;
using System.Linq;

namespace AsmFea.Extract
{
    public static class Rematch
    {
        /// <summary>Normals or axes must agree to this cosine (about 2.5 degrees). Guess.</summary>
        public const double ORIENT_COS = 0.999;
        /// <summary>Area may differ by this fraction (the design may have changed a little). Guess.</summary>
        public const double AREA_RTOL = 0.25;
        /// <summary>Largest accepted score (centroid shift in face sizes plus relative area change). Guess.</summary>
        public const double MAX_SCORE = 0.5;
        /// <summary>The runner-up must score at least this many times worse for the best to be "clear". Guess.</summary>
        public const double CLEAR_FACTOR = 3.0;

        /// <summary>
        /// How far a candidate fingerprint is from the stored one: infinity when it cannot be
        /// the same face (other surface type, orientation or radius, or area far off), else the
        /// centroid shift divided by the face's size plus the relative area change.
        /// </summary>
        public static double Score(JObj stored, JObj candidate)
        {
            if ((string)stored["surface"] != (string)candidate["surface"]) return double.PositiveInfinity;
            foreach (var key in new[] { "normal", "axis" })
            {
                if (!stored.Has(key) || !candidate.Has(key)) continue;
                double cos = Vec(stored[key]).Unit().Dot(Vec(candidate[key]).Unit());
                // a cylinder's axis may flip sign; a plane's outward normal may not
                if ((key == "axis" ? Math.Abs(cos) : cos) < ORIENT_COS) return double.PositiveInfinity;
            }
            if (stored.Has("radius") && candidate.Has("radius"))
            {
                double r0 = Num(stored["radius"]), r1 = Num(candidate["radius"]);
                if (Math.Abs(r1 - r0) > 0.01 * r0) return double.PositiveInfinity;
            }
            double a0 = Num(stored["area"]), a1 = Num(candidate["area"]);
            double dA = Math.Abs(a1 - a0) / a0;
            if (dA > AREA_RTOL) return double.PositiveInfinity;
            double size = Math.Sqrt(a0);
            double dc = (Vec(stored["centroid"]) - Vec(candidate["centroid"])).Length / size;
            return dc + dA;
        }

        /// <summary>
        /// The index of the one clear match among candidate scores, or -1 with the reason:
        /// no candidate close enough, or two that are about as close as each other.
        /// </summary>
        public static int Pick(IList<double> scores, out string reason)
        {
            var order = Enumerable.Range(0, scores.Count).Where(i => scores[i] <= MAX_SCORE)
                                  .OrderBy(i => scores[i]).ToList();
            if (order.Count == 0)
            {
                reason = "no face of the part matches its type, orientation and size";
                return -1;
            }
            if (order.Count > 1 && scores[order[1]] < CLEAR_FACTOR * Math.Max(scores[order[0]], 1e-6))
            {
                reason = order.Count + " faces of the part match about equally well";
                return -1;
            }
            reason = "";
            return order[0];
        }

        private static Vec3 Vec(object o)
        {
            var a = o as double[];
            if (a != null) return Vec3.From(a);
            var l = (IList<object>)o;
            return new Vec3(Num(l[0]), Num(l[1]), Num(l[2]));
        }

        private static double Num(object o) { return Convert.ToDouble(o); }
    }
}
