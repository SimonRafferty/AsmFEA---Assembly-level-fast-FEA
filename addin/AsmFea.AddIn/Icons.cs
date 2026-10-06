// The AsmFEA icon: AsmFEA_Icon.png (a meshed hexagon in stress-plot colours), embedded in the
// DLL by build.ps1 and scaled to each size SolidWorks asks for. Without the resource, a
// fallback is drawn in code: a cantilever beam bending under load, coloured like a stress plot
// (red at the root, where bending stress is highest, through to blue at the free tip).
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;

namespace AsmFea.AddIn
{
    public static class Icons
    {
        private const string RESOURCE = "AsmFea.AddIn.Icon.png";
        private static Image master;

        /// <summary>The icon at size x size pixels (the embedded image, else the drawn fallback);
        /// transparent background unless one is given.</summary>
        public static Bitmap Icon(int size, Color? background = null)
        {
            if (master == null)
                using (var st = typeof(Icons).Assembly.GetManifestResourceStream(RESOURCE))
                    if (st != null) master = new Bitmap(Image.FromStream(st));   // a copy: the stream closes
            if (master == null) return Draw(size, background);
            var bmp = new Bitmap(size, size, PixelFormat.Format32bppArgb);
            using (var g = Graphics.FromImage(bmp))
            {
                g.Clear(background ?? Color.Transparent);
                g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                g.SmoothingMode = SmoothingMode.AntiAlias;
                g.DrawImage(master, new Rectangle(0, 0, size, size));
            }
            return bmp;
        }

        /// <summary>The drawn fallback icon at size x size pixels.</summary>
        public static Bitmap Draw(int size, Color? background = null)
        {
            var bmp = new Bitmap(size, size, PixelFormat.Format32bppArgb);
            using (var g = Graphics.FromImage(bmp))
            {
                g.SmoothingMode = SmoothingMode.AntiAlias;
                g.Clear(background ?? Color.Transparent);
                float s = size;
                float wallW = Math.Max(2f, s * 0.14f);
                float x0 = wallW, x1 = s * 0.96f;                   // beam root and tip
                float top = s * 0.18f, depth = Math.Max(4f, s * (size <= 24 ? 0.28f : 0.2f));
                float sag = s * 0.42f;                              // tip deflection

                // undeformed beam (grey outline)
                if (size >= 32)
                    using (var pen = new Pen(Color.FromArgb(150, 128, 128, 128), Math.Max(1f, s / 64f)) { DashStyle = DashStyle.Dash })
                        g.DrawRectangle(pen, x0, top, x1 - x0, depth);

                // deformed beam: thin slices, each coloured by its bending stress
                int n = Math.Max(24, size);
                Func<float, float> v = t => sag * t * t * (3f - t) / 2f;     // cantilever with an end load
                for (int i = 0; i < n; i++)
                {
                    float ta = i / (float)n, tb = (i + 1) / (float)n;
                    float xa = x0 + ta * (x1 - x0), xb = x0 + tb * (x1 - x0) + 0.6f;   // overlap: no seams
                    var quad = new[]
                    {
                        new PointF(xa, top + v(ta)), new PointF(xb, top + v(tb)),
                        new PointF(xb, top + v(tb) + depth), new PointF(xa, top + v(ta) + depth),
                    };
                    using (var b = new SolidBrush(Rainbow(1f - (ta + tb) / 2f)))
                        g.FillPolygon(b, quad);
                }
                // a dark edge round the deformed beam reads well at small sizes
                var edge = new List<PointF>();
                for (int i = 0; i <= n; i++) { float t = i / (float)n; edge.Add(new PointF(x0 + t * (x1 - x0), top + v(t))); }
                for (int i = n; i >= 0; i--) { float t = i / (float)n; edge.Add(new PointF(x0 + t * (x1 - x0), top + v(t) + depth)); }
                using (var pen = new Pen(Color.FromArgb(200, 40, 40, 40), Math.Max(0.8f, s / 48f)))
                    g.DrawPolygon(pen, edge.ToArray());

                // the wall it is built into
                using (var wall = new SolidBrush(Color.FromArgb(255, 70, 70, 70)))
                    g.FillRectangle(wall, 0, s * 0.06f, wallW, s * 0.88f);
                if (size >= 40)
                    using (var hatch = new Pen(Color.FromArgb(255, 160, 160, 160), s / 64f))
                        for (float y = s * 0.1f; y < s * 0.9f; y += s / 10f)
                            g.DrawLine(hatch, 0, y + wallW, wallW, y);
            }
            return bmp;
        }

        /// <summary>Stress-plot colours: 0 = blue, 1 = red.</summary>
        private static Color Rainbow(float t)
        {
            t = Math.Max(0f, Math.Min(1f, t));
            float h = (1f - t) * 240f / 60f;                 // hue 240 (blue) .. 0 (red), in sextants
            float x = 1f - Math.Abs(h % 2f - 1f);
            float r, g, b;
            if (h < 1) { r = 1; g = x; b = 0; }
            else if (h < 2) { r = x; g = 1; b = 0; }
            else if (h < 3) { r = 0; g = 1; b = x; }
            else { r = 0; g = x; b = 1; }
            return Color.FromArgb(255, (int)(r * 255), (int)(g * 255), (int)(b * 255));
        }

        /// <summary>PNG files of the icon at each size, written to the temp folder.</summary>
        public static string[] Pngs(params int[] sizes)
        {
            var paths = new string[sizes.Length];
            for (int i = 0; i < sizes.Length; i++)
            {
                paths[i] = Path.Combine(Path.GetTempPath(), "asmfea_icon_" + sizes[i] + ".png");
                using (var bmp = Icon(sizes[i])) bmp.Save(paths[i], ImageFormat.Png);
            }
            return paths;
        }
    }
}
