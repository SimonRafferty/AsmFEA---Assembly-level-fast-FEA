using System;
using System.Collections.Generic;
using System.Linq;

namespace AsmFea.Extract
{
    /// <summary>
    /// The assembly's X, Y and Z axes as they look in the current view, for the load dialog's triad (the user,
    /// 2026-10-18: "hard to know the part orientation otherwise"). Pure, so it can be tested without SolidWorks.
    /// </summary>
    public static class Triad
    {
        public struct Axis
        {
            public int Index;        // 0 X, 1 Y, 2 Z
            public double Right;     // the axis' screen components: right, up (each -1..1)
            public double Up;
            public double Toward;    // + toward the viewer, - away
        }

        /// <summary>
        /// The three axes on screen from the view's toward-viewer and up directions (assembly frame, as
        /// IPaneHost.CurrentView gives them), farthest first so the nearer ones draw over them; null for a
        /// degenerate view.
        /// </summary>
        public static List<Axis> Project(IList<double> toward, IList<double> up)
        {
            if (toward == null || up == null || toward.Count != 3 || up.Count != 3) return null;
            var z = Unit(toward);
            var y = Unit(up);
            if (z == null || y == null) return null;
            var x = Cross(y, z);                                      // screen right = up x toward (right-handed)
            return Enumerable.Range(0, 3)
                             .Select(i => new Axis { Index = i, Right = x[i], Up = y[i], Toward = z[i] })
                             .OrderBy(a => a.Toward)
                             .ToList();
        }

        /// <summary>
        /// The axes for a view as IPaneHost.CurrentView gives it ({toward_viewer, up}). The vectors come as double[]
        /// from SolidWorks (Extractor.ActiveViewOrientation) and as lists once through JSON: both are read (a double[]
        /// is no IEnumerable&lt;object&gt;, which hid the triad live).
        /// </summary>
        public static List<Axis> FromView(JObj view)
        {
            return view == null ? null : Project(Numbers(view["toward_viewer"]), Numbers(view["up"]));
        }

        private static IList<double> Numbers(object o)
        {
            var e = o as System.Collections.IEnumerable;
            if (e == null || o is string) return null;
            try { return e.Cast<object>().Select(c => Convert.ToDouble(c, System.Globalization.CultureInfo.InvariantCulture)).ToList(); }
            catch (Exception) { return null; }
        }

        private static double[] Unit(IList<double> v)
        {
            double n = Math.Sqrt(v.Sum(c => c * c));
            return n < 1e-9 ? null : v.Select(c => c / n).ToArray();
        }

        private static double[] Cross(double[] a, double[] b)
        {
            return new[] { a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0] };
        }
    }
}
