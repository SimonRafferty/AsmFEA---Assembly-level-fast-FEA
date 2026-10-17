// A progress bar that fills blue while a job runs and turns solid green when it is done (the user, 2026-10-16:
// green means go). The Windows bar is green throughout with visual styles; its colour cannot be set.
using System;
using System.Drawing;
using System.Windows.Forms;

namespace AsmFea.AddIn
{
    public class ColourBar : Control
    {
        public static readonly Color Working = Color.FromArgb(0, 120, 215), Done = Color.FromArgb(16, 160, 60),
                                     Stale = Color.FromArgb(205, 40, 40);
        private readonly Timer marquee = new Timer { Interval = 40 };
        private ProgressBarStyle style = ProgressBarStyle.Continuous;
        private int value, offset;

        public ColourBar()
        {
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint
                     | ControlStyles.ResizeRedraw, true);
            marquee.Tick += (s, e) => { offset = (offset + 4) % Math.Max(Width, 1); Invalidate(); };
        }

        /// <summary>Continuous: filled to Value (0-100); Marquee: a block moving to and fro (no count known).</summary>
        public ProgressBarStyle Style
        {
            get { return style; }
            set
            {
                style = value;
                marquee.Enabled = value == ProgressBarStyle.Marquee;
                Invalidate();
            }
        }

        private bool stale;

        /// <summary>Solid red: what this bar stands for is out of date (connections to analyse again, a study to run).</summary>
        public bool NeedsRun
        {
            get { return stale; }
            set { if (stale != value) { stale = value; Invalidate(); } }
        }

        public int Value
        {
            get { return value; }
            set { this.value = Math.Max(0, Math.Min(100, value)); Invalidate(); }
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            var r = ClientRectangle;
            using (var back = new SolidBrush(Color.FromArgb(230, 230, 230))) g.FillRectangle(back, r);
            var inner = Rectangle.Inflate(r, -1, -1);
            if (stale && style != ProgressBarStyle.Marquee)
            {
                using (var b = new SolidBrush(Stale)) g.FillRectangle(b, inner);
            }
            else if (style == ProgressBarStyle.Marquee)
            {
                int w = Math.Max(inner.Width / 4, 8);
                int x = inner.X + offset - w;
                using (var b = new SolidBrush(Working))
                    g.FillRectangle(b, Rectangle.Intersect(inner, new Rectangle(x, inner.Y, w, inner.Height)));
            }
            else if (value > 0)
            {
                using (var b = new SolidBrush(value >= 100 ? Done : Working))
                    g.FillRectangle(b, new Rectangle(inner.X, inner.Y, inner.Width * value / 100, inner.Height));
            }
            using (var pen = new Pen(Color.FromArgb(188, 188, 188))) g.DrawRectangle(pen, r.X, r.Y, r.Width - 1, r.Height - 1);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) marquee.Dispose();
            base.Dispose(disposing);
        }
    }
}
