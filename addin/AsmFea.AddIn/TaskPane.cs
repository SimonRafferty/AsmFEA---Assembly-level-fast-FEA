// The AsmFEA task pane (PLAN.md "Task pane"): Study, Loads and fixtures, Connections, Run,
// Results. It edits the active assembly's studies (stored in the document), exports through
// the extractor, and drives the pipeline through the CLI, polling job.json. It never blocks
// SolidWorks: CLI calls run on worker threads, polling on a UI timer.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Windows.Forms;
using AsmFea.Extract;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;

namespace AsmFea.AddIn
{
    /// <summary>What the pane needs from the add-in.</summary>
    public interface IPaneHost
    {
        IModelDoc2 ActiveModel { get; }
        StudyStore StoreFor(IModelDoc2 doc);
        /// <summary>The assembly's largest bounding-box dimension, mm (0 when unknown).</summary>
        double AssemblySizeMm(IModelDoc2 doc);
        void MarkChanged(IModelDoc2 doc);
        string StudyFolder(IModelDoc2 doc, string study);
        /// <summary>The active view of the document: {toward_viewer, up}, assembly frame; null without a view.</summary>
        JObj CurrentView(IModelDoc2 doc);
        string Export(IModelDoc2 doc, string study, out IList<string> log);
        JObj FaceRefFromSelection(IModelDoc2 doc, IFace2 face, string id);
        int HighlightFaces(IModelDoc2 doc, JObj study, IEnumerable<string> faceIds);
        void SelectComponents(IModelDoc2 doc, IEnumerable<string> partIds);
        IList<IFace2> SelectedFaces(IModelDoc2 doc);
        /// <summary>
        /// Show a temporary arrow (not part of the document) along <paramref name="direction"/>
        /// with its tip at <paramref name="tipMm"/> (assembly frame, mm); with <paramref name="markTip"/>
        /// (a remote force) the point gets a marker and the arrow starts there instead; tip null puts it at the
        /// middle of the assembly. direction null removes the arrow.
        /// </summary>
        /// <returns>null when drawn (or removed), else why it could not be drawn.</returns>
        string ShowArrow(IModelDoc2 doc, double[] tipMm, double[] direction, bool markTip);
    }

    public class StudyPane : UserControl
    {
        private readonly IPaneHost host;
        private readonly Label asmLabel = new Label { AutoSize = true, Padding = new Padding(2, 6, 2, 2) };
        private readonly ComboBox studyBox = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 150 };
        private readonly TabControl tabs = new PaneTabs { Dock = DockStyle.Fill };

        // Study tab
        private readonly NumericUpDown bucklingModes = new NumericUpDown { Minimum = 1, Maximum = 20, Value = 5, Width = 60 };
        private readonly ComboBox fidelity = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 100 };
        private readonly CheckBox convergence = new CheckBox { Text = "Convergence check (solve twice)", AutoSize = true };
        private readonly CheckBox fastLoads = new CheckBox { Text = "Fast load-size changes", AutoSize = true };
        private readonly TextBox maxDefl = new TextBox { Width = 70 }, minFos = new TextBox { Width = 70 }, minBuck = new TextBox { Width = 70 };

        // Loads tab, connections, run, results
        private readonly ListView loadsList = MakeList("Id", "Kind", "Type", "Value", "Faces");
        // the load case the Loads tab shows and adds to; its buckling tick
        private readonly ComboBox caseBox = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 170 };
        private readonly CheckBox caseBuckling = new CheckBox { Text = "Buckling", AutoSize = true, Padding = new Padding(0, 3, 0, 0) };
        private bool showing;          // true while ShowStudy fills the controls: their change events must not store

        /// <summary>A load case in the drop-down: its name, marked when it includes buckling.</summary>
        private class CaseItem
        {
            public string Name;
            public bool Buckling;
            public override string ToString() { return Name + (Buckling ? "   (+ buckling)" : ""); }
        }

        // Pass/fail criteria given to a study that has none (mirrors asmfea.defaults: PANE_MIN_FOS etc.)
        private const double DefaultMinFos = 1.5, DefaultMinBucklingFactor = 3.0, DefaultDeflectionRatio = 250;
        private readonly ListView connList = MakeList("Connection", "Found by");
        private TabPage connTab, resTab;
        private readonly CheckBox exceptionsOnly = new CheckBox { Text = "Exceptions only", AutoSize = true };
        private readonly TextBox bondTol = new TextBox { Width = 45 };
        private readonly ComboBox solveCase = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 150 };
        private readonly CheckBox fullRun = new CheckBox { Text = "Full re-run", AutoSize = true, Padding = new Padding(8, 3, 0, 0) };
        private readonly ProgressBar progress = new ProgressBar { Dock = DockStyle.Top, Height = 14 };
        private readonly Label activity = new Label { Dock = DockStyle.Top, Height = 34, AutoEllipsis = true };
        private readonly Label connActivity = new Label { Dock = DockStyle.Top, Height = 34, AutoEllipsis = true };   // the same text, on the Connections tab
        private readonly Label resActivity = new Label { Dock = DockStyle.Top, Height = 34, AutoEllipsis = true };    // and on the Results tab
        // shown over fast-run results, so a screenshot of the pane can't be taken for a full result
        private Button reportBtn, summaryBtn;   // disabled over fast-run results: reports need a full run
        private readonly Label fastBanner = new Label { Dock = DockStyle.Top, AutoSize = false, Height = 54, Visible = false,
            Text = "APPROXIMATE: fast run. For iterative design only; do a full run before relying on these results or producing a report.",
            BackColor = Color.FromArgb(255, 140, 0), ForeColor = Color.White, Font = new Font("Segoe UI", 8.5f, FontStyle.Bold),
            Padding = new Padding(4, 3, 4, 3) };
        private readonly Label loadsActivity = new Label { Dock = DockStyle.Bottom, Height = 34, AutoEllipsis = true }; // and on the Loads tab
        private readonly ComboBox plotCase = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 90 };
        private readonly ListView problems = MakeList("Severity", "Code", "What");
        private readonly TextBox logBox = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, Font = new Font(FontFamily.GenericMonospace, 8f) };
        private readonly ListView checksList = MakeList("Check", "Value", "Limit", "Status");
        private readonly TextBox keyFigures = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill };
        private readonly Timer poll = new Timer { Interval = 1000 };
        private string jobId;
        private string jobFile;        // the running job's job.json, fixed when it starts (not the active document's)
        private static readonly ToolTip tips = new ToolTip { AutoPopDelay = 20000, InitialDelay = 500, ReshowDelay = 200, ShowAlways = true };

        /// <summary>Tooltip text for every button, by its caption.</summary>
        /// <summary>How to use the 3-D windows (the windows themselves carry no instructions, D44).</summary>
        private const string ViewerUse =
            "\n\nIn the window:" +
            "\n  Left drag: rotate.  Wheel: zoom.  Right drag: pan (as in SolidWorks).  R: back to the main view." +
            "\n  Click the model: the part and its values at that point.  Click the background: clear them." +
            "\n  Play (bottom left): ramp the load up and down; click again to stop at the full load." +
            "\n  Undeformed shape: a grey outline of the original shape.  Deformation x: exaggerate the deformation (0: none)." +
            "\n  Min / max (bottom right): show or hide the smallest and largest values.";

        private static readonly Dictionary<string, string> ButtonTips = new Dictionary<string, string>
        {
            { "New", "Create a new study in this assembly (saved with the assembly)." },
            { "Delete", "Delete the selected item. On the Study row: delete the whole study from the assembly (its results folder is kept)." },
            { "New load case", "Add a load case: a set of loads solved together. Each case is solved separately in the same run." },
            { "Rename", "Rename the selected load case (its loads move with it)." },
            { "Delete load case", "Delete the selected load case and its loads (the fixtures stay; they belong to every case)." },
            { "Fixed", "Loads and fixtures: hold the selected faces completely (no movement in any direction). Connections: bond the two parts together where they touch." },
            { "Pinned", "Hold the selected faces against translation but let them rotate (cylindrical faces: a pin in a hole)." },
            { "Force", "A force on the selected faces: X, Y and Z components in newtons (assembly axes), the total shared over all of them. "
                       + "To make it act at a point off the faces (a lever arm, a torque), give that point's X, Y, Z distance from the faces' centre; 0, 0, 0 = on the faces." },
            { "Pressure", "A pressure in MPa pushing into the selected faces." },
            { "Gravity", "Self-weight of the whole assembly: the acceleration as X, Y, Z components in mm/s² (no faces needed)." },
            { "Analyse connections", "Export the model and find how the parts connect (the same as Check only on the Run tab). The list fills when it finishes." },
            { "Sliding with friction...", "Separating contact with a friction coefficient: the faces can slide when friction is overcome, and can lift apart. Slower to solve." },
            { "Sliding joint...", "The parts stay together but slide freely (no friction) along one direction you choose." },
            { "Not connected (delete)", "Delete this connection: the parts do not transfer load here. 'Automatic' brings it back." },
            { "Automatic", "Remove your choice and let AsmFEA decide the connection from the geometry and mates." },
            { "?", "Explain the selected item in full (or double-click it)." },
            { "More Info", "Explain the selected problem in full: what it means and what to do (or double-click it)." },
            { "Export", "Write the model (model.json and one STEP file per part) into the study folder, without running anything." },
            { "Run", "Export, then check, mesh, solve (the load cases chosen in Solve) and report. Progress shows below; the job runs in the background." },
            { "Fast run", "Fast run: for iterative design only. Results are approximate. Do a full run at the end, before relying on the results or producing a report." },
            { "Check only", "Export and check the model (connections, materials, free motion) without meshing or solving. Fast." },
            { "Cancel", "Stop the running job." },
            { "Open folder", "Open the study folder (exported model, run files, results and report) in Explorer." },
            { "Open report", "Open the full Word report (for the Technical File); it is rewritten first if the results are newer." },
            { "Open summary", "Open the half-page summary (project details, a stress plot, key results) for risk assessments; it is rewritten first if the results are newer." },
            { "Stress", "Open the von Mises stress of the last run in a 3-D window." + ViewerUse },
            { "Displacement", "Open the displacement (deformed shape) of the last run in a 3-D window." + ViewerUse },
            { "FoS", "Open the factor of safety (yield / von Mises) of the last run in a 3-D window." + ViewerUse },
            { "Open in PrePoMax", "Open the last run's results in PrePoMax (free) for a closer look. The first time, AsmFEA helps you find or download PrePoMax." },
        };

        private static Control Tip(Control c, string text)
        {
            tips.SetToolTip(c, text);
            return c;
        }
        private List<JObj> shownDiagnostics = new List<JObj>();

        public StudyPane(IPaneHost host)
        {
            this.host = host;
            Dock = DockStyle.Fill;
            var top = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, WrapContents = true };
            top.Controls.Add(asmLabel);
            var row = new FlowLayoutPanel { AutoSize = true, WrapContents = false };
            row.Controls.Add(new Label { Text = "Study", AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
            row.Controls.Add(studyBox);
            row.Controls.Add(Btn("New", NewStudy));
            row.Controls.Add(Btn("Delete", DeleteStudy));
            top.Controls.Add(row);
            tabs.TabPages.Add(StudyTab());
            tabs.TabPages.Add(LoadsTab());
            tabs.TabPages.Add(ConnectionsTab());
            tabs.TabPages.Add(RunTab());
            tabs.TabPages.Add(ResultsTab());
            Controls.Add(tabs);
            Controls.Add(top);
            Tip(studyBox, "The study shown in the tabs below. An assembly can hold several studies.");
            Tip(bucklingModes, "How many buckling modes to find for each load case that includes buckling.");
            Tip(caseBox, "The load case shown below; new loads are added to it. '(+ buckling)' marks the cases checked for buckling.");
            Tip(caseBuckling, "Also check this load case for buckling: the factor by which its loads can grow before the structure buckles. "
                             + "Adds a buckling solve to the run, so it takes longer. Static stresses and deflections are always solved.");
            Tip(fidelity, "Mesh size: coarse is quick for checking set-up; fine gives more accurate local stresses but takes longer.");
            Tip(convergence, "Solve on a base mesh and a finer one and report how much the results changed. Doubles the solve time.");
            Tip(fastLoads, "Solve each load on its own once; a load case is then the sum of its loads. Changing only the size of "
                         + "loads afterwards needs no new solve, just the results (exact for linear static studies). The first solve "
                         + "takes longer when a case has several loads. Not used with buckling, separating contact or preloaded bolts.");
            Tip(maxDefl, "Pass/fail limit on the largest deflection, in mm (default: the assembly's largest dimension / 250). Blank: no check.");
            Tip(minFos, "Pass/fail minimum factor of safety on yield (default 1.5). Blank: no check.");
            Tip(minBuck, "Pass/fail minimum buckling factor (default 3: linear buckling over-predicts real collapse loads). Blank: no check.");
            Tip(loadsList, "Loads and fixtures in this study. Select one to highlight its faces in SolidWorks (a load also shows an arrow where it acts); double-click it to edit.");
            Tip(connList, "How the parts connect, from the last check or run: the connection and the parts it joins, and how it was found. "
                         + "Select a row to highlight the parts; double-click it (or press More Info) for the details.");
            Tip(exceptionsOnly, "Show only the connection problems that need a decision.");
            Tip(solveCase, "Which load cases Run solves: all of them, or just one (each case adds to the solve time).");
            Tip(fullRun, "Redo every stage for the next Run, ignoring everything kept from earlier runs. Normally a stage whose "
                       + "inputs have not changed reuses its earlier result (the run log says what was reused). Clears itself after the run starts.");
            Tip(bondTol, "Faces this close count as touching and are bonded: a gap, or an overlap, of up to this many mm "
                       + "(default 0.5). Raise it for parts placed with small gaps rather than mated. Click Analyse connections to apply.");
            Tip(problems, "Problems found by the last run. Select one to highlight where it is; double-click it (or press ?) for a full explanation.");
            Tip(logBox, "Progress and results of the running job, and the end of the solver log.");
            Tip(checksList, "Pass/fail checks against the limits on the Study tab.");
            Tip(keyFigures, "Key results for each load case.");
            Tip(plotCase, "The load case the plot buttons show.");
            // the Connections and Results tabs read the last run whenever they are shown
            Action showTab = () =>
            {
                if (tabs.SelectedTab == connTab) LoadConnections();
                else if (tabs.SelectedTab == resTab) WithCurrentView(null);
                // a load's arrow belongs to the Loads tab
                if (tabs.SelectedTab != null && !tabs.SelectedTab.Controls.Contains(loadsList))
                    try { host.ShowArrow(Doc, null, null, false); } catch { }
            };
            studyBox.SelectedIndexChanged += (s, e) => { ShowStudy(); showTab(); };
            tabs.SelectedIndexChanged += (s, e) => showTab();
            poll.Tick += (s, e) => PollJob();
        }

        // ------------------------------------------------------------------ helpers

        private static ListView MakeList(params string[] cols)
        {
            var lv = new ListView { View = System.Windows.Forms.View.Details, FullRowSelect = true, Dock = DockStyle.Fill, HideSelection = false };
            foreach (var c in cols) lv.Columns.Add(c, -2);
            return lv;
        }

        private static Button Btn(string text, Action click)
        {
            var b = new Button { Text = text, AutoSize = true };
            string tip;
            if (ButtonTips.TryGetValue(text, out tip)) tips.SetToolTip(b, tip);
            b.Click += (s, e) => click();
            return b;
        }

        private static void Fit(ListView lv)
        {
            foreach (ColumnHeader c in lv.Columns) c.Width = -2;
        }

        private void Say(string text, bool error = false)
        {
            loadsActivity.Text = resActivity.Text = connActivity.Text = activity.Text = text;
            loadsActivity.ForeColor = resActivity.ForeColor = connActivity.ForeColor = activity.ForeColor = error ? Color.Firebrick : SystemColors.ControlText;
            // the line is cut to fit the pane: the whole message is in its tooltip
            foreach (var l in new[] { activity, connActivity, resActivity, loadsActivity }) tips.SetToolTip(l, text);
        }

        private IModelDoc2 Doc { get { return host.ActiveModel; } }
        private string StudyName { get { return studyBox.SelectedItem as string; } }
        private JObj Study
        {
            get
            {
                var doc = Doc;
                return doc == null || StudyName == null ? null : host.StoreFor(doc).Get(StudyName);
            }
        }

        private static string Fmt(object v)
        {
            if (v == null) return "";
            if (v is double) return ((double)v).ToString("G6", CultureInfo.InvariantCulture);
            var l = v as System.Collections.IEnumerable;                 // a list, or a double[] made this session
            if (l != null && !(v is string)) return string.Join(", ", l.Cast<object>().Select(Fmt));
            return Convert.ToString(v, CultureInfo.InvariantCulture);
        }

        private static double? Num(string s)
        {
            double v;
            return double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out v) ? v : (double?)null;
        }

        private static List<object> Csv(string s)
        {
            return s.Split(',').Select(x => x.Trim()).Where(x => x.Length > 0).Cast<object>().ToList();
        }

        // ------------------------------------------------------------------ refresh

        /// <summary>Called when the active document changes.</summary>
        /// <summary>An internal error in a pane callback: shown on the status line, with the detail in its tooltip.</summary>
        public void ReportFault(Exception e)
        {
            Say("Internal error (the pane carries on): " + e.GetType().Name + ": " + e.Message + System.Environment.NewLine + e.StackTrace, true);
        }

        /// <summary>
        /// Select a tab by its title; with a path, draw the pane into a PNG there (documentation
        /// screenshots). The pane is drawn at width x height; when SolidWorks is not showing it, it is
        /// laid out at that size just for the picture. Returns the size drawn, "w x h".
        /// </summary>
        public string Screenshot(string tab, string path, int width = 400, int height = 640)
        {
            var page = tabs.TabPages.Cast<TabPage>().FirstOrDefault(t => string.Equals(t.Text, tab, StringComparison.OrdinalIgnoreCase));
            if (page == null) throw new ArgumentException("no tab " + tab);
            if (tabs.SelectedTab != page) tabs.SelectedTab = page;
            Application.DoEvents();
            if (path == null) return Width + " x " + Height;
            var old = Size;
            try
            {
                Size = new Size(width, height);
                PerformLayout();
                page.PerformLayout();
                Refresh();
                Application.DoEvents();
                using (var bmp = new Bitmap(width, height))
                {
                    DrawToBitmap(bmp, new Rectangle(0, 0, width, height));
                    bmp.Save(path, System.Drawing.Imaging.ImageFormat.Png);
                }
            }
            finally { Size = old; }
            return width + " x " + height;
        }

        public void RefreshDocument()
        {
            var doc = Doc;
            studyBox.Items.Clear();
            if (doc == null)
            {
                // no assembly or part active (closed, or a drawing in front): nothing of the last one stays
                ClearAll();
                asmLabel.Text = "Open an assembly or a part to set up a study.";
                Enabled = true;
                return;
            }
            asmLabel.Text = (doc.GetType() == (int)swDocumentTypes_e.swDocPART ? "Part: " : "Assembly: ") + doc.GetTitle();
            var store = host.StoreFor(doc);
            foreach (var n in store.Names) studyBox.Items.Add(n);
            if (studyBox.Items.Count > 0) studyBox.SelectedIndex = 0;
            else ShowStudy();
        }

        /// <summary>Empty every field (the assembly the pane showed was closed, found live).</summary>
        private void ClearAll()
        {
            showing = true;
            try
            {
                studyBox.Items.Clear();
                loadsList.Items.Clear();
                connList.Items.Clear();
                problems.Items.Clear();
                shownDiagnostics = new List<JObj>();
                checksList.Items.Clear();
                keyFigures.Text = logBox.Text = "";
                caseBox.Items.Clear();
                solveCase.Items.Clear();
                plotCase.Items.Clear();
                foreach (var tb in new[] { maxDefl, minFos, minBuck, bondTol }) tb.Text = "";
                caseBuckling.Checked = convergence.Checked = fastLoads.Checked = false;
                progress.Style = ProgressBarStyle.Continuous;
                progress.Value = 0;
                Say("");
            }
            finally { showing = false; }
        }

        private void ShowStudy()
        {
            var st = Study;
            showing = true;
            try
            {
                loadsList.Items.Clear();
                if (st == null) { caseBox.Items.Clear(); return; }
                EnsureCriteria(st);
                var s = Settings(st);
                bucklingModes.Value = s["buckling_modes"] == null ? 5 : Convert.ToDecimal(s["buckling_modes"]);
                var mesh = s["mesh"] as JObj ?? new JObj();
                fidelity.SelectedItem = (string)mesh["fidelity"] ?? "standard";
                convergence.Checked = mesh["convergence_check"] == null || (bool)mesh["convergence_check"];
                fastLoads.Checked = s["load_superposition"] != null && (bool)s["load_superposition"];
                var lim = s["limits"] as JObj ?? new JObj();
                maxDefl.Text = Fmt(lim["max_deflection"]);
                minFos.Text = Fmt(lim["min_fos"]);
                minBuck.Text = Fmt(lim["min_buckling_factor"]);
                foreach (var tb in new[] { maxDefl, minFos, minBuck, bondTol }) tb.BackColor = SystemColors.Window;
                bondTol.Text = s["bond_tolerance"] != null ? Fmt(s["bond_tolerance"]) : "0.5";      // the pipeline's default (GAP_TOL)
                bucklingModes.Enabled = minBuck.Enabled = BucklingOn(st);
                ShowCases(st, CurrentCaseName);
                ShowLoads(st);
                string chosen = solveCase.SelectedItem as string;
                solveCase.Items.Clear();
                solveCase.Items.Add("All load cases");
                foreach (var c in Cases(st)) solveCase.Items.Add(c);
                int at = chosen == null ? -1 : solveCase.Items.IndexOf(chosen);
                solveCase.SelectedIndex = at >= 0 ? at : 0;
            }
            finally { showing = false; }
        }

        /// <summary>The fixtures (every case) and the loads of the selected load case.</summary>
        private void ShowLoads(JObj st)
        {
            loadsList.Items.Clear();
            if (st == null) return;
            string lc = CurrentCaseName;
            foreach (JObj fx in StudyStore.List(st, "fixtures").OfType<JObj>())
                loadsList.Items.Add(new ListViewItem(new[] { (string)fx["id"], "fixture", (string)fx["type"], FixtureValue(fx), Fmt(fx["faces"]) }) { Tag = fx });
            foreach (JObj ld in StudyStore.List(st, "loads").OfType<JObj>().Where(l => (string)l["load_case"] == lc))
                loadsList.Items.Add(new ListViewItem(new[] { (string)ld["id"], "load", (string)ld["type"], LoadValue(ld), Fmt(ld["faces"]) }) { Tag = ld });
            Fit(loadsList);
        }

        // ------------------------------------------------------------------ study settings and load cases
        //
        // Static is always solved. Buckling is per load case: "analyses" holds "buckling" exactly when
        // at least one case is in "buckling_cases" (written in full; the pipeline reads an empty list
        // as all cases). Every change is stored at once: there is no Apply.

        private static JObj Settings(JObj st)
        {
            var s = st["study"] as JObj;
            if (s == null) st["study"] = s = new JObj();
            return s;
        }

        private static List<string> Cases(JObj st)
        {
            var l = StudyStore.List(Settings(st), "load_cases");
            if (l.Count == 0) l.Add("LC1");
            return l.Cast<string>().ToList();
        }

        private static bool BucklingOn(JObj st)
        {
            var an = Settings(st)["analyses"] as List<object>;
            return an != null && an.Contains("buckling");
        }

        private static HashSet<string> BucklingCases(JObj st)
        {
            if (!BucklingOn(st)) return new HashSet<string>();
            var bc = (Settings(st)["buckling_cases"] as List<object>)?.Cast<string>().ToList();
            return new HashSet<string>(bc != null && bc.Count > 0 ? bc : Cases(st));
        }

        private static void SetBuckling(JObj st, HashSet<string> cases)
        {
            var s = Settings(st);
            var keep = Cases(st).Where(cases.Contains).ToList();
            s["analyses"] = keep.Count > 0 ? new List<object> { "static", "buckling" } : new List<object> { "static" };
            if (keep.Count > 0) s["buckling_cases"] = keep.Cast<object>().ToList();
            else s.Remove("buckling_cases");
        }

        private string CurrentCaseName { get { return (caseBox.SelectedItem as CaseItem)?.Name; } }

        /// <summary>Fill the load-case drop-down, keeping <paramref name="select"/> selected when it still exists.</summary>
        private void ShowCases(JObj st, string select)
        {
            bool was = showing;
            showing = true;
            try
            {
                var bc = BucklingCases(st);
                caseBox.BeginUpdate();
                caseBox.Items.Clear();
                foreach (var c in Cases(st)) caseBox.Items.Add(new CaseItem { Name = c, Buckling = bc.Contains(c) });
                caseBox.EndUpdate();
                int at = caseBox.Items.Cast<CaseItem>().ToList().FindIndex(c => c.Name == select);
                caseBox.SelectedIndex = at >= 0 ? at : 0;
                caseBuckling.Checked = bc.Contains(CurrentCaseName ?? "");
            }
            finally { showing = was; }
        }

        /// <summary>Stored in the study (no Apply); the list and drop-downs are not rebuilt, so typing is not disturbed.</summary>
        private void Stored()
        {
            host.MarkChanged(Doc);
            Say("Stored in the study. Save the assembly to keep it.");
        }

        /// <summary>A study with no pass/fail criteria gets the defaults (shown on the Study tab; blank a box for no check).</summary>
        private void EnsureCriteria(JObj st)
        {
            var s = Settings(st);
            if (s["limits"] != null) return;
            var lim = new JObj { { "min_fos", DefaultMinFos }, { "min_buckling_factor", DefaultMinBucklingFactor } };
            double size = 0;
            try { size = host.AssemblySizeMm(Doc); } catch { }
            if (size > 0) lim["max_deflection"] = RoundSig(size / DefaultDeflectionRatio, 2);
            s["limits"] = lim;
            host.MarkChanged(Doc);
            Trace("pass/fail criteria defaulted: " + Json.Write(lim));
        }

        private static double RoundSig(double v, int digits)
        {
            if (v <= 0) return v;
            double scale = Math.Pow(10, Math.Floor(Math.Log10(v)) - (digits - 1));
            return Math.Round(v / scale) * scale;
        }

        private bool WarnBuckling()
        {
            return MessageBox.Show(this, "Buckling adds a buckling solve for each load case that includes it, so runs take longer "
                                   + "(typically half as long again, up to twice as long).\n\nUntick Buckling on any load case to leave it out.",
                                   "AsmFEA: include buckling",
                                   MessageBoxButtons.OKCancel, MessageBoxIcon.Information) == DialogResult.OK;
        }

        private void CaseBucklingChanged()
        {
            var st = Study;
            if (showing || st == null || CurrentCaseName == null) return;
            if (caseBuckling.Checked && !BucklingOn(st) && !WarnBuckling())
            {
                showing = true; caseBuckling.Checked = false; showing = false;
                return;
            }
            var bc = BucklingCases(st);
            if (caseBuckling.Checked) bc.Add(CurrentCaseName); else bc.Remove(CurrentCaseName);
            SetBuckling(st, bc);
            bucklingModes.Enabled = minBuck.Enabled = BucklingOn(st);
            ShowCases(st, CurrentCaseName);
            Stored();
        }

        private void NewCase()
        {
            var st = Study;
            if (st == null) { Say("Create a study first (New).", true); return; }
            var cases = Cases(st);
            string name = Prompt("New load case", "Load case name:", "LC" + (cases.Count + 1));
            if (name == null) return;
            name = name.Trim();
            if (!ValidCaseName(name, cases, null)) return;
            StudyStore.List(Settings(st), "load_cases").Add(name);
            if (BucklingOn(st)) { var bc = BucklingCases(st); bc.Add(name); SetBuckling(st, bc); }   // checked for buckling when others are
            ShowCases(st, name);
            ShowLoads(st);
            Stored();
        }

        private void RenameCase()
        {
            var st = Study;
            string old = CurrentCaseName;
            if (st == null || old == null) return;
            string name = Prompt("Rename load case", "New name for " + old + ":", old);
            if (name == null) return;
            name = name.Trim();
            if (name == old || !ValidCaseName(name, Cases(st), old)) return;
            var bc = BucklingCases(st);
            var list = StudyStore.List(Settings(st), "load_cases");
            list[list.IndexOf(old)] = name;
            foreach (JObj ld in StudyStore.List(st, "loads").OfType<JObj>().Where(l => (string)l["load_case"] == old)) ld["load_case"] = name;
            if (bc.Remove(old)) bc.Add(name);
            SetBuckling(st, bc);
            ShowCases(st, name);
            ShowLoads(st);
            Stored();
        }

        private void DeleteCase()
        {
            var st = Study;
            string lc = CurrentCaseName;
            if (st == null || lc == null) return;
            var cases = Cases(st);
            if (cases.Count == 1) { Say("A study needs at least one load case: add another before deleting this one.", true); return; }
            var loads = StudyStore.List(st, "loads").OfType<JObj>().Where(l => (string)l["load_case"] == lc).ToList();
            if (MessageBox.Show(this, "Delete load case " + lc + (loads.Count > 0 ? " and its " + loads.Count + " load(s)?" : "?"), "AsmFEA",
                                MessageBoxButtons.OKCancel) != DialogResult.OK) return;
            var bc = BucklingCases(st);
            foreach (var ld in loads) StudyStore.List(st, "loads").Remove(ld);
            StudyStore.List(Settings(st), "load_cases").Remove(lc);
            bc.Remove(lc);
            if (BucklingOn(st)) SetBuckling(st, bc);
            StudyStore.Prune(st);
            bucklingModes.Enabled = minBuck.Enabled = BucklingOn(st);
            ShowCases(st, null);
            ShowLoads(st);
            Stored();
        }

        private bool ValidCaseName(string name, List<string> cases, string except)
        {
            string problem = name.Length == 0 ? "A load case needs a name."
                           : cases.Any(c => c != except && string.Equals(c, name, StringComparison.OrdinalIgnoreCase)) ? "There is already a load case called " + name + "."
                           : null;
            if (problem != null) Say(problem, true);
            return problem == null;
        }

        /// <summary>The load's X, Y, Z components (magnitude x direction, as stored).</summary>
        private static double[] Components(JObj ld)
        {
            double m = ld["magnitude"] == null ? 0 : Convert.ToDouble(ld["magnitude"], CultureInfo.InvariantCulture);
            var d = Nums(ld["direction"]);
            return d == null || d.Length != 3 ? null : d.Select(v => m * v).ToArray();
        }

        private static string G(double v) { return Math.Round(v, 9).ToString("G6", CultureInfo.InvariantCulture); }

        /// <summary>The Value column: components and resultant (force), the offset of a remote force.</summary>
        private static string LoadValue(JObj ld)
        {
            string type = (string)ld["type"];
            if (type == "pressure") return Fmt(ld["magnitude"]) + " MPa";
            var c = Components(ld);
            if (c == null) return Fmt(ld["magnitude"]);
            string unit = type == "gravity" ? " mm/s²" : " N";
            string s = string.Join(", ", c.Select(G)) + unit + " (" + G(Math.Sqrt(c.Sum(v => v * v))) + unit + ")";
            if (type == "remote_force")
                s += ld["point"] != null ? " at " + Fmt(ld["point"]) + " mm"
                   : " at " + Fmt(ld["offset"] ?? new List<object> { 0.0, 0.0, 0.0 }) + " mm from the faces' centroid";
            return s;
        }

        private static string FixtureValue(JObj fx)
        {
            Func<object, string> axes = v => string.Join("", ((List<object>)v).Select(a => "XYZ"[Convert.ToInt32(a) - 1].ToString()));
            var parts = new List<string>();
            if (fx["dofs"] != null) parts.Add("holds " + axes(fx["dofs"]));
            if (fx["hold_rotations"] != null && ((List<object>)fx["hold_rotations"]).Count > 0) parts.Add("no turning about " + axes(fx["hold_rotations"]));
            return string.Join("; ", parts);
        }

        private void Changed()
        {
            host.MarkChanged(Doc);
            ShowStudy();
            Say("Changed. Save the assembly to keep the study in it.");
        }

        // ------------------------------------------------------------------ studies

        private void NewStudy()
        {
            if (Doc == null) return;
            string name = Prompt("New study", "Study name:", "Study" + (studyBox.Items.Count + 1));
            if (string.IsNullOrWhiteSpace(name)) return;
            if (name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0) { Say("A study name must be a valid folder name.", true); return; }
            host.StoreFor(Doc).GetOrCreate(name.Trim());
            host.MarkChanged(Doc);
            RefreshDocument();
            studyBox.SelectedItem = name.Trim();
        }

        private void DeleteStudy()
        {
            if (Study == null) return;
            if (MessageBox.Show("Delete study " + StudyName + " from this assembly? (Its results folder is kept.)", "AsmFEA",
                                MessageBoxButtons.OKCancel) != DialogResult.OK) return;
            host.StoreFor(Doc).Remove(StudyName);
            host.MarkChanged(Doc);
            RefreshDocument();
        }

        // ------------------------------------------------------------------ Study tab

        private TabPage StudyTab()
        {
            var p = new TabPage("Study") { AutoScroll = true };
            var t = new TableLayoutPanel { ColumnCount = 2, AutoSize = true, Dock = DockStyle.Top, Padding = new Padding(4) };
            Action<string, Control> add = (label, c) =>
            {
                t.Controls.Add(new Label { Text = label, AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
                t.Controls.Add(c);
            };
            Action<string> heading = text =>
            {
                var h = new Label { Text = text, AutoSize = true, Font = new Font(Font, FontStyle.Bold), Padding = new Padding(0, 12, 0, 2) };
                t.Controls.Add(h);
                t.SetColumnSpan(h, 2);
            };
            fidelity.Items.AddRange(new object[] { "coarse", "standard", "fine" });
            add("Mesh fidelity", fidelity);
            add("", convergence);
            add("", fastLoads);
            heading("Pass / fail criteria (used in the reports)");
            add("Max deflection, mm", maxDefl);
            add("Min factor of safety (yield)", minFos);
            add("Min buckling factor", minBuck);
            p.Controls.Add(t);

            // every change is stored at once (no Apply)
            bucklingModes.ValueChanged += (s, e) => { if (!showing && Study != null) { Settings(Study)["buckling_modes"] = (int)bucklingModes.Value; Stored(); } };
            Action storeMesh = () =>
            {
                if (showing || Study == null) return;
                Settings(Study)["mesh"] = new JObj { { "fidelity", (string)fidelity.SelectedItem ?? "standard" }, { "convergence_check", convergence.Checked } };
                Stored();
            };
            fidelity.SelectedIndexChanged += (s, e) => storeMesh();
            convergence.CheckedChanged += (s, e) => storeMesh();
            fastLoads.CheckedChanged += (s, e) =>
            {
                if (showing || Study == null) return;
                if (fastLoads.Checked) Settings(Study)["load_superposition"] = true; else Settings(Study).Remove("load_superposition");
                Stored();
            };
            foreach (var pair in new[] { Tuple.Create(maxDefl, "max_deflection"), Tuple.Create(minFos, "min_fos"), Tuple.Create(minBuck, "min_buckling_factor") })
            {
                var tb = pair.Item1;
                string key = pair.Item2;
                tb.TextChanged += (s, e) =>
                {
                    if (showing || Study == null) return;
                    var lim = Settings(Study)["limits"] as JObj;
                    if (lim == null) Settings(Study)["limits"] = lim = new JObj();
                    var v = Num(tb.Text);
                    bool blank = tb.Text.Trim().Length == 0;
                    tb.BackColor = blank || v > 0 ? SystemColors.Window : Color.MistyRose;   // not a positive number: not stored
                    if (blank) lim.Remove(key);
                    else if (v > 0) lim[key] = v.Value;
                    else return;
                    Stored();
                };
            }
            return p;
        }

        // ------------------------------------------------------------------ Loads and fixtures

        private TabPage LoadsTab()
        {
            var p = new TabPage("Loads and fixtures");
            var cases = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(0, 2, 0, 4) };
            cases.Controls.Add(new Label { Text = "Load case", AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
            cases.Controls.Add(caseBox);
            // "[x] Buckling [5] modes" kept together when the row wraps
            var buck = new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = new Padding(0) };
            bucklingModes.Width = 45;
            buck.Controls.Add(caseBuckling);
            buck.Controls.Add(bucklingModes);
            buck.Controls.Add(new Label { Text = "modes", AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
            cases.Controls.Add(buck);
            var caseButtons = new FlowLayoutPanel { AutoSize = true, Margin = new Padding(0) };
            caseButtons.Controls.Add(Btn("New load case", NewCase));
            caseButtons.Controls.Add(Btn("Rename", RenameCase));
            caseButtons.Controls.Add(Btn("Delete load case", DeleteCase));
            cases.Controls.Add(caseButtons);
            // rows: the case; its buckling; the case buttons
            cases.SetFlowBreak(caseBox, true);
            cases.SetFlowBreak(buck, true);
            caseBox.SelectedIndexChanged += (s, e) =>
            {
                if (showing || Study == null) return;
                showing = true;
                caseBuckling.Checked = BucklingCases(Study).Contains(CurrentCaseName ?? "");
                showing = false;
                ShowLoads(Study);
            };
            caseBuckling.CheckedChanged += (s, e) => CaseBucklingChanged();
            var bar = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true };
            var add = new Label { Text = "From the selected faces, add:", AutoSize = true, Padding = new Padding(0, 4, 0, 0) };
            bar.Controls.Add(add);
            bar.SetFlowBreak(add, true);                                // the buttons on the line below
            bar.Controls.Add(Btn("Fixed", () => AddFixture("fixed")));
            bar.Controls.Add(Btn("Pinned", () => AddFixture("pinned")));
            bar.Controls.Add(Btn("Force", () => AddLoad("force")));
            bar.Controls.Add(Btn("Pressure", () => AddLoad("pressure")));
            bar.Controls.Add(Btn("Gravity", () => AddLoad("gravity")));
            var delete = Btn("Delete", DeleteLoad);
            delete.Margin = new Padding(18, delete.Margin.Top, delete.Margin.Right, delete.Margin.Bottom);   // a gap: harder to hit by accident
            bar.Controls.Add(delete);
            loadsList.SelectedIndexChanged += (s, e) => HighlightSelected();
            loadsList.DoubleClick += (s, e) => EditSelected();
            p.Controls.Add(loadsList);
            p.Controls.Add(loadsActivity);
            p.Controls.Add(bar);
            // a line between the load-case controls and what is added to the case
            var rule = new Panel { Dock = DockStyle.Top, Height = 9 };
            rule.Paint += (s, e) =>
            {
                using (var pen = new Pen(Color.FromArgb(0xb8, 0xc4, 0xd4))) e.Graphics.DrawLine(pen, 4, 4, rule.Width - 5, 4);
            };
            rule.Resize += (s, e) => rule.Invalidate();
            p.Controls.Add(rule);
            p.Controls.Add(cases);
            return p;
        }

        /// <summary>Face refs for the faces selected in the graphics area, added to the study.</summary>
        private List<object> FacesFromSelection(JObj st)
        {
            var faces = host.SelectedFaces(Doc);
            if (faces.Count == 0) throw new InvalidOperationException("Select one or more faces in the graphics area first.");
            var ids = new List<object>();
            foreach (var f in faces)
            {
                var fr = host.FaceRefFromSelection(Doc, f, StudyStore.NextId(st, "face_refs", "L"));
                var same = StudyStore.List(st, "face_refs").OfType<JObj>()
                           .FirstOrDefault(o => (string)o["persistent_ref"] == (string)fr["persistent_ref"]);
                if (same != null) { ids.Add(same["id"]); continue; }
                StudyStore.List(st, "face_refs").Add(fr);
                ids.Add(fr["id"]);
            }
            return ids;
        }

        /// <summary>True when faces are selected in SolidWorks; otherwise says so in a pop-up.</summary>
        private bool FacesSelected()
        {
            if (host.SelectedFaces(Doc).Count > 0) return true;
            MessageBox.Show(this, "Select a face or faces on the model first, then click the button again.", "AsmFEA",
                            MessageBoxButtons.OK, MessageBoxIcon.Information);
            return false;
        }

        private void AddFixture(string type)
        {
            var st = Study;
            if (st == null) { Say("Create a study first (New).", true); return; }
            if (!FacesSelected()) return;
            try
            {
                var fx = new JObj { { "id", StudyStore.NextId(st, "fixtures", "FX") }, { "type", type }, { "faces", FacesFromSelection(st) } };
                StudyStore.List(st, "fixtures").Add(fx);
                Changed();
            }
            catch (Exception e) { Say(e.Message, true); }
        }

        private void AddLoad(string type)
        {
            var st = Study;
            if (st == null) { Say("Create a study first (New).", true); return; }
            if (type != "gravity" && !FacesSelected()) return;
            string lc = CurrentCaseName ?? Cases(st)[0];
            using (var dlg = new LoadDialog(type, lc, null))
            {
                if (dlg.ShowDialog(this) != DialogResult.OK) return;
                try
                {
                    var ld = new JObj { { "id", StudyStore.NextId(st, "loads", "LD") }, { "load_case", lc }, { "type", type } };
                    if (type != "gravity") ld["faces"] = FacesFromSelection(st);
                    dlg.WriteTo(ld);
                    StudyStore.List(st, "loads").Add(ld);
                    Changed();
                    Reselect((string)ld["id"]);
                }
                catch (Exception e) { Say(e.Message, true); }
            }
        }

        /// <summary>Double-click: edit the selected load (values, case) or fixture (type, held directions); the faces stay.</summary>
        private void EditSelected()
        {
            var st = Study;
            bool isLoad;
            var item = SelectedLoadItem(st, out isLoad);
            if (item == null) return;
            try
            {
                if (!isLoad)
                {
                    using (var dlg = new FixtureDialog(item))
                    {
                        if (dlg.ShowDialog(this) != DialogResult.OK) return;
                        dlg.WriteTo(item);
                    }
                }
                else
                {
                    using (var dlg = new LoadDialog((string)item["type"], (string)item["load_case"], item))
                    {
                        if (dlg.ShowDialog(this) != DialogResult.OK) return;
                        dlg.WriteTo(item);
                    }
                }
                Changed();
                Reselect((string)item["id"]);
            }
            catch (Exception e) { Say(e.Message, true); }
        }

        /// <summary>Select a row by id again after the list was refilled (shows its faces and arrow).</summary>
        private void Reselect(string id)
        {
            foreach (ListViewItem row in loadsList.Items)
                if (row.Text == id) { row.Selected = true; row.EnsureVisible(); return; }
        }

        private void DeleteLoad()
        {
            var st = Study;
            bool isLoad;
            var item = SelectedLoadItem(st, out isLoad);
            if (item == null) return;
            StudyStore.List(st, isLoad ? "loads" : "fixtures").Remove(item);
            StudyStore.Prune(st);
            Changed();
        }

        private void HighlightSelected()
        {
            try
            {
                if (Study == null || loadsList.SelectedItems.Count == 0)
                {
                    Trace("selection: none (study " + (Study == null ? "missing" : "ok") + ")");
                    Trace("ShowArrow(clear) -> " + (host.ShowArrow(Doc, null, null, false) ?? "ok"));
                    return;
                }
                bool isLoad;
                var item = SelectedLoadItem(Study, out isLoad);
                if (item == null) return;
                Trace("selection: " + item["id"] + (isLoad ? " load " : " fixture ") + item["type"]);
                var faces = item["faces"] as List<object>;
                if (faces != null) Trace("highlighted " + host.HighlightFaces(Doc, Study, faces.Cast<string>()) + " of " + faces.Count + " faces");
                ShowLoadArrow(Study, item, isLoad);
            }
            catch (Exception e) { Trace("error: " + e); Say(e.Message, true); }
        }

        /// <summary>
        /// The selected row's load or fixture, found by id in the study as it is NOW. The row's Tag
        /// can be a stale copy when the study was re-read from the document after the list was
        /// filled; an edit written to it was lost (found live). Null, and the list refilled, when
        /// the item is no longer in the study.
        /// </summary>
        private JObj SelectedLoadItem(JObj st, out bool isLoad)
        {
            isLoad = false;
            if (st == null || loadsList.SelectedItems.Count == 0) return null;
            var row = loadsList.SelectedItems[0];
            isLoad = row.SubItems[1].Text == "load";
            string id = row.Text;
            var item = StudyStore.List(st, isLoad ? "loads" : "fixtures").OfType<JObj>().FirstOrDefault(o => (string)o["id"] == id);
            if (item == null)
            {
                Trace("row " + id + " is no longer in the study: list refilled");
                BeginInvoke(new Action(ShowStudy));
            }
            else if (!ReferenceEquals(item, row.Tag))
                Trace("row " + id + " held a stale copy (the study was re-read since the list was filled)");
            return item;
        }

        /// <summary>A line in the temp folder's asmfea_pane.log (the load arrow's steps, for diagnosis).</summary>
        internal static void Trace(string line)
        {
            try
            {
                string log = Path.Combine(Path.GetTempPath(), "asmfea_pane.log");
                if (File.Exists(log) && new FileInfo(log).Length > 256 * 1024) File.Delete(log);   // start again rather than grow
                File.AppendAllText(log, DateTime.Now.ToString("HH:mm:ss.fff ") + line + System.Environment.NewLine);
            }
            catch { }
        }

        /// <summary>Automation (diagnosis): select a row of the loads list as a click would; returns the status line.</summary>
        public string SelectLoadRow(string id)
        {
            tabs.SelectedTab = tabs.TabPages.Cast<TabPage>().First(t => t.Controls.Contains(loadsList));
            loadsList.SelectedItems.Clear();
            Application.DoEvents();
            foreach (ListViewItem row in loadsList.Items)
                if (row.Text == id) { row.Selected = true; Application.DoEvents(); return loadsActivity.Text; }
            return "no row " + id + " (rows: " + string.Join(", ", loadsList.Items.Cast<ListViewItem>().Select(r => r.Text)) + ")";
        }

        /// <summary>
        /// The arrow for a selected load, pointing along it with its tip where it acts: the
        /// area-weighted centroid of its faces (from their fingerprints), moved by a remote force's
        /// offset (with a link back to the centroid). Pressure points into the faces (planar faces
        /// only); gravity sits in the middle of the assembly. Fixtures show no arrow.
        /// </summary>
        private void ShowLoadArrow(JObj st, JObj item, bool isLoad)
        {
            string type = (string)item["type"];
            if (!isLoad) { Trace("a fixture: no arrow; clear -> " + (host.ShowArrow(Doc, null, null, false) ?? "ok")); Say(""); return; }
            var ids = new HashSet<string>(((item["faces"] as List<object>) ?? new List<object>()).Cast<string>());
            Vec3 sum = new Vec3(0, 0, 0), pushIn = new Vec3(0, 0, 0);
            double area = 0;
            foreach (JObj fr in StudyStore.List(st, "face_refs").OfType<JObj>())
            {
                var fp = fr["fingerprint"] as JObj;
                if (!ids.Contains((string)fr["id"]) || fp == null || fp["centroid"] == null) continue;
                double a = Convert.ToDouble(fp["area"] ?? 1.0, CultureInfo.InvariantCulture);
                sum = sum + V(fp["centroid"]) * a;
                area += a;
                if (fp["normal"] != null) pushIn = pushIn - V(fp["normal"]) * a;
            }
            double[] tip = null;
            bool markTip = false;
            if (type != "gravity")
            {
                Trace("faces " + ids.Count + ", area " + area + ", centroid sum " + string.Join(",", sum.ToArray().Select(v => G(v))));
                if (area <= 0)
                {
                    host.ShowArrow(Doc, null, null, false);
                    Say((string)item["id"] + ": no arrow (its faces have no recorded position).", true);
                    return;
                }
                var c = sum * (1.0 / area);
                tip = c.ToArray();
                if (type == "remote_force")
                {
                    var p = item["point"] != null ? V(item["point"]) : c + (item["offset"] != null ? V(item["offset"]) : new Vec3(0, 0, 0));
                    markTip = true;
                    tip = p.ToArray();
                }
            }
            Vec3 dir;
            if (type == "pressure")
            {
                double m = Convert.ToDouble(item["magnitude"] ?? 1.0, CultureInfo.InvariantCulture);
                dir = pushIn * Math.Sign(m);                  // a negative pressure pulls the faces out
            }
            else
            {
                var comp = Components(item);
                dir = comp == null ? new Vec3(0, 0, 0) : Vec3.From(comp);
            }
            if (dir.Length < 1e-12)
            {
                host.ShowArrow(Doc, null, null, false);
                Say((string)item["id"] + ": no arrow (no direction" + (type == "pressure" ? " for a pressure on curved faces)." : ")."));
                return;
            }
            // the outcome goes on the status line: where the arrow is, or why it could not be drawn
            string err = host.ShowArrow(Doc, tip, dir.Unit().ToArray(), markTip);
            Trace("ShowArrow tip " + (tip == null ? "middle" : string.Join(",", tip.Select(v => G(v)))) + " dir "
                  + string.Join(",", dir.Unit().ToArray().Select(v => G(v))) + " -> " + (err ?? "ok"));
            if (err != null) Say((string)item["id"] + ": could not draw the arrow: " + err, true);
            else Say((string)item["id"] + ": arrow along (" + string.Join(", ", dir.Unit().ToArray().Select(v => G(v))) + ")"
                     + (tip == null ? ", in the middle of the assembly." : ", acting at (" + string.Join(", ", tip.Select(v => G(v))) + ") mm."));
        }

        private static Vec3 V(object v)
        {
            var a = Nums(v);
            return new Vec3(a[0], a[1], a[2]);
        }

        /// <summary>
        /// The numbers of a JSON array however it is held: a list once read from the document, but a
        /// double[] in a face ref made this session (an arrow for a new force failed on that, found live).
        /// </summary>
        private static double[] Nums(object v)
        {
            var e = v as System.Collections.IEnumerable;
            if (e == null || v is string) return null;
            return e.Cast<object>().Select(x => Convert.ToDouble(x, CultureInfo.InvariantCulture)).ToArray();
        }

        // ------------------------------------------------------------------ Connections

        private TabPage ConnectionsTab()
        {
            var p = connTab = new TabPage("Connections");
            var bar = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true };
            // the same as Check only on the Run tab: export, then resolve (the list refills when it finishes)
            bar.Controls.Add(Btn("Analyse connections", () => Run("resolve")));
            bar.Controls.Add(exceptionsOnly);
            bar.Controls.Add(Btn("?", ExplainConnection));
            exceptionsOnly.CheckedChanged += (s, e) => LoadConnections();
            connList.DoubleClick += (s, e) => ExplainConnection();
            // Overrides for the selected connection (stored in the study, used from the next run)
            var edit = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true };
            edit.Controls.Add(new Label { Text = "Set to:", AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
            edit.Controls.Add(Btn("Fixed", () => SetConnection("tie", null, null)));
            edit.Controls.Add(Btn("Sliding with friction...", SetContact));
            edit.Controls.Add(Btn("Sliding joint...", SetSliding));
            edit.Controls.Add(Btn("Not connected (delete)", () => SetConnection("none", null, null)));
            edit.Controls.Add(Btn("Automatic", ClearConnection));
            connList.SelectedIndexChanged += (s, e) =>
            {
                var row = SelectedConnection();
                if (row != null) host.SelectComponents(Doc, row.Parts);
            };
            // the bond tolerance: faces within it (a gap or an overlap) count as touching
            var tol = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true };
            tol.Controls.Add(new Label { Text = "Bond gaps and overlaps up to", AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
            tol.Controls.Add(bondTol);
            tol.Controls.Add(new Label { Text = "mm", AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
            bondTol.TextChanged += (s, e) =>
            {
                if (showing || Study == null) return;
                var v = Num(bondTol.Text);
                bool blank = bondTol.Text.Trim().Length == 0;
                bondTol.BackColor = blank || v >= 0 ? SystemColors.Window : Color.MistyRose;
                if (blank) Settings(Study).Remove("bond_tolerance");
                else if (v >= 0) Settings(Study)["bond_tolerance"] = v.Value;
                else return;
                Stored();
                Say("Bond tolerance stored: click Analyse connections to apply it.");
            };
            p.Controls.Add(connList);
            p.Controls.Add(connActivity);
            p.Controls.Add(edit);
            p.Controls.Add(tol);
            p.Controls.Add(bar);
            return p;
        }

        /// <summary>A Connections row: the two parts and, for a found interface, its id.</summary>
        private class ConnRow
        {
            public List<string> Parts;
            public string Interface;
            public JObj Connection;        // the connections.json record, for a found interface
            public JObj Diagnostic;        // the exception, in Exceptions only
        }

        private void ExplainConnection()
        {
            var row = SelectedConnection();
            if (row == null) { Say("Select a connection first.", true); return; }
            if (row.Diagnostic != null) { ShowHelp(row.Diagnostic); return; }
            var c = row.Connection;
            if (c == null) return;
            var sb = new StringBuilder();
            sb.AppendLine("Connection " + (string)c["id"] + " between " + string.Join(" and ", row.Parts));
            sb.AppendLine();
            sb.AppendLine("Type: " + Describe((string)c["type"], c["friction"], c["params"] as JObj));
            sb.AppendLine("Decided by: " + Fmt(c["source"]));
            var ev = c["evidence"] as JObj;
            if (ev != null && ev["rule"] != null) sb.AppendLine("Why: " + Fmt(ev["rule"]));
            if (ev != null && ev["mates"] != null) sb.AppendLine("Mates: " + Fmt(ev["mates"]));
            if (c["geometry"] != null) sb.AppendLine("Touching faces: " + Fmt(c["geometry"]) + (c["area"] == null ? "" : ", " + Fmt(c["area"]) + " mm2") + (c["gap"] == null ? "" : ", gap " + Fmt(c["gap"]) + " mm"));
            sb.AppendLine();
            sb.AppendLine("The choices (Set to):");
            sb.AppendLine("  Fixed: the touching faces are bonded together and move as one. The usual choice for welded, glued or tightly clamped parts.");
            sb.AppendLine("  Sliding with friction: the faces press on each other and can slide once friction is overcome, or lift apart. Most realistic for parts resting on each other, but slower to solve; a part held only this way gets weak stabilising springs (stated in the report).");
            sb.AppendLine("  Sliding joint: the parts stay together but slide freely along one direction. Something else must stop the slide, or the model is a mechanism.");
            sb.AppendLine("  Not connected: no load passes through this interface.");
            sb.AppendLine("  Automatic: AsmFEA decides from the geometry and mates (the default).");
            sb.AppendLine();
            sb.AppendLine("Weld beads (Insert > Assembly Feature > Weld Bead in SolidWorks) bond the touching faces within the bead size of the weld path; the rest of the interface keeps the type set here. A weld on both sides or all around bonds the whole interface.");
            sb.AppendLine();
            sb.Append("A change is stored in the study and used from the next run; save the assembly to keep it.");
            ShowText("Connection " + (string)c["id"], sb.ToString());
        }

        private ConnRow SelectedConnection()
        {
            return connList.SelectedItems.Count == 0 ? null : connList.SelectedItems[0].Tag as ConnRow;
        }

        /// <summary>A part's name as the feature tree shows it: the body of a weldment, the part of a sub-assembly.</summary>
        private static string ShortName(string id)
        {
            int b = id.IndexOf("/body:", StringComparison.Ordinal);
            if (b >= 0) return id.Substring(b + 6);
            int k = id.LastIndexOf('/');
            return k >= 0 ? id.Substring(k + 1) : id;
        }

        private static string PartsLabel(IEnumerable<string> parts)
        {
            return string.Join(" + ", parts.Select(ShortName));
        }

        /// <summary>The connection type in a word or two, for the list.</summary>
        private static string ShortType(string type, object friction, JObj prms)
        {
            switch (type)
            {
                case "tie": return "Fixed";
                case "contact": return "Contact (mu " + Fmt(friction ?? (prms == null ? null : prms["friction"]) ?? 0.0) + ")";
                case "sliding":
                    var d = prms == null ? null : prms["direction"] as List<object>;
                    return "Sliding" + (d == null ? "" : " " + AxisName(d));
                case "revolute": return "Pin";
                case "bolted": return "Bolted";
                case "none": return "Not connected";
                default: return Humanise(type);
            }
        }

        /// <summary>How a connection was found, in a few words.</summary>
        private static string FoundBy(JObj c)
        {
            switch ((string)c["source"])
            {
                case "weldment": return c["butt"] != null ? "weldment butt" : (string)c["geometry"] == "line" ? "weldment edge" : "weldment";
                case "geometry" when c["butt"] != null: return "touching, gap bridged";
                case "sub-assembly" when c["butt"] != null: return "sub-assembly, gap bridged";
                case "geometry": return (string)c["geometry"] == "line" ? "edge contact" : "touching faces";
                case "sub-assembly": return "rigid sub-assembly";
                case "override": return "your choice";
                case "weld": return "weld bead";
                case "mate": return "mate";
                default: return Fmt(c["source"]);
            }
        }

        /// <summary>"LINE_CONTACT" -> "Line contact".</summary>
        private static string Humanise(string code)
        {
            if (string.IsNullOrEmpty(code)) return "";
            string t = code.Replace('_', ' ').ToLowerInvariant();
            return char.ToUpperInvariant(t[0]) + t.Substring(1);
        }

        private static string Describe(string type, object friction, JObj prms)
        {
            if (type == "tie") return "fixed (bonded)";
            if (type == "contact") return "sliding with friction (mu " + Fmt(friction ?? (prms == null ? null : prms["friction"]) ?? 0.0) + ")";
            if (type == "sliding")
            {
                var d = prms == null ? null : prms["direction"] as List<object>;
                return "sliding joint" + (d == null ? "" : " along " + AxisName(d));
            }
            if (type == "none") return "not connected";
            return type;
        }

        private static string AxisName(List<object> d)
        {
            var v = d.Select(x => Convert.ToDouble(x, CultureInfo.InvariantCulture)).ToList();
            if (v.Count != 3) return Fmt(d);
            for (int i = 0; i < 3; i++)
                if (Math.Abs(v[i]) > 0.999) return "XYZ".Substring(i, 1);
            return Fmt(d);
        }

        /// <summary>The stored override that applies to this row: one for its interface, else one for the whole part pair.</summary>
        private static JObj OverrideFor(JObj st, ConnRow row)
        {
            var pair = new HashSet<string>(row.Parts);
            var ovs = StudyStore.List(st, "overrides").OfType<JObj>()
                      .Where(o => pair.SetEquals(((o["parts"] as List<object>) ?? new List<object>()).Cast<string>())).ToList();
            return ovs.FirstOrDefault(o => row.Interface != null && (string)o["interface"] == row.Interface)
                   ?? ovs.FirstOrDefault(o => o["interface"] == null);
        }

        private void LoadConnections()
        {
            connList.Items.Clear();
            if (Doc == null || StudyName == null) return;
            string folder = host.StudyFolder(Doc, StudyName);
            if (exceptionsOnly.Checked)
            {
                Cli.RunAsync(this, "exceptions " + Cli.Quote(folder), (r, err) =>
                {
                    if (err != null) { Say(err, true); return; }
                    foreach (JObj d in ((((JObj)r)["exceptions"]) as List<object> ?? new List<object>()).OfType<JObj>())
                    {
                        var where = d["where"] as JObj;
                        var parts = ((where == null ? null : where["parts"] as List<object>) ?? new List<object>()).Cast<string>().ToList();
                        var conns = ((where == null ? null : where["connections"] as List<object>) ?? new List<object>()).Cast<string>().ToList();
                        connList.Items.Add(new ListViewItem(new[] { Humanise((string)d["code"]) + ": " + PartsLabel(parts), (string)d["severity"] })
                        { Tag = new ConnRow { Parts = parts, Interface = conns.Count == 1 ? conns[0] : null, Diagnostic = d } });
                    }
                    Fit(connList);
                });
                return;
            }
            string path = Path.Combine(folder, "connections.json");
            if (!File.Exists(path))
            {
                connList.Items.Add(new ListViewItem(new[] { "No connections yet: click Analyse connections.", "" }));
                Fit(connList);
                return;
            }
            JObj doc;
            try { doc = (JObj)Json.Parse(File.ReadAllText(path, Encoding.UTF8)); }
            catch (Exception e) { Say("Could not read connections.json: " + e.Message, true); return; }
            var st = Study;
            foreach (JObj c in ((doc["connections"] as List<object>) ?? new List<object>()).OfType<JObj>())
            {
                var row = new ConnRow { Parts = ((c["parts"] as List<object>) ?? new List<object>()).Cast<string>().ToList(), Interface = (string)c["id"], Connection = c };
                string type = ShortType((string)c["type"], c["friction"], c["params"] as JObj);
                string source = FoundBy(c);
                if ((string)c["source"] == "weld")
                {
                    // a weld bead (edited in SolidWorks, not here)
                    var region = c["region"] as JObj;
                    type = "welded" + (region != null ? " (" + Fmt(region["width"]) + " mm strip)" : " (whole interface)");
                    connList.Items.Add(new ListViewItem(new[] { "Welded: " + PartsLabel(row.Parts), "weld bead" }) { Tag = row });
                    continue;
                }
                // an override changed since the last run: show what the next run will use
                var ov = st == null ? null : OverrideFor(st, row);
                if (ov != null)
                {
                    string next = ShortType((string)ov["connection"], ov["friction"], ov["params"] as JObj);
                    if (next != type || (string)c["source"] != "override") { type = next; source = "your choice (next run)"; }
                }
                else if ((string)c["source"] == "override")
                    source = "automatic (next run)";
                connList.Items.Add(new ListViewItem(new[] { type + ": " + PartsLabel(row.Parts), source }) { Tag = row });
            }
            Fit(connList);
        }

        private void SetContact()
        {
            if (SelectedConnection() == null) { Say("Select a connection first.", true); return; }
            string mu = Prompt("Sliding with friction", "Friction coefficient (0 = frictionless; faces may separate)", "0.2");
            if (mu == null) return;
            var v = Num(mu);
            if (v == null || v < 0) { Say("Give a friction coefficient of 0 or more.", true); return; }
            SetConnection("contact", v, null);
        }

        private void SetSliding()
        {
            if (SelectedConnection() == null) { Say("Select a connection first.", true); return; }
            string axis = Choose("Sliding joint", "Slide along (assembly axis)", new[] { "X", "Y", "Z" });
            if (axis == null) return;
            var d = new List<object> { axis == "X" ? 1.0 : 0.0, axis == "Y" ? 1.0 : 0.0, axis == "Z" ? 1.0 : 0.0 };
            SetConnection("sliding", null, new JObj { { "direction", d } });
        }

        /// <summary>Store an override for the selected connection in the study; it is used from the next run.</summary>
        private void SetConnection(string type, double? friction, JObj prms)
        {
            var st = Study;
            var row = SelectedConnection();
            if (st == null || row == null) { Say("Select a connection first.", true); return; }
            if (row.Parts.Count != 2) { Say("That problem does not concern one pair of parts; select a connection between two parts.", true); return; }
            if (row.Connection != null && (string)row.Connection["source"] == "weld")
            { Say("That connection is a weld bead: edit or suppress the weld bead in SolidWorks.", true); return; }
            var ovs = StudyStore.List(st, "overrides");
            ovs.RemoveAll(o => o is JObj && OverrideMatches((JObj)o, row));
            var ov = new JObj { { "id", "OV-" + (row.Interface ?? string.Join("-", row.Parts)) }, { "parts", row.Parts.Cast<object>().ToList() } };
            if (row.Interface != null) ov["interface"] = row.Interface;
            ov["connection"] = type;
            if (friction != null) ov["friction"] = friction.Value;
            if (prms != null) ov["params"] = prms;
            ovs.Add(ov);
            Changed();
            LoadConnections();
            Say("Connection set to " + Describe(type, friction, prms) + ". Run the study to use it.");
        }

        private void ClearConnection()
        {
            var st = Study;
            var row = SelectedConnection();
            if (st == null || row == null) { Say("Select a connection first.", true); return; }
            int n = StudyStore.List(st, "overrides").RemoveAll(o => o is JObj && OverrideMatches((JObj)o, row));
            if (n == 0) { Say("That connection has no override."); return; }
            Changed();
            LoadConnections();
            Say("Connection back to automatic. Run the study to use it.");
        }

        private static bool OverrideMatches(JObj o, ConnRow row)
        {
            var parts = ((o["parts"] as List<object>) ?? new List<object>()).Cast<string>();
            return new HashSet<string>(row.Parts).SetEquals(parts) && (string)o["interface"] == row.Interface;
        }

        // ------------------------------------------------------------------ Run

        private TabPage RunTab()
        {
            var p = new TabPage("Run");
            var bar = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true };
            bar.Controls.Add(Btn("Run", () => Run(null)));
            bar.Controls.Add(Btn("Fast run", () => Run(null, true)));
            bar.Controls.Add(Btn("Check only", () => Run("resolve")));
            bar.Controls.Add(Btn("Cancel", CancelJob));
            bar.Controls.Add(Btn("More Info", () => { if (problems.SelectedItems.Count > 0) ShowHelp((JObj)problems.SelectedItems[0].Tag); else Say("Select a problem first.", true); }));
            problems.DoubleClick += (s, e) => { if (problems.SelectedItems.Count > 0) ShowHelp((JObj)problems.SelectedItems[0].Tag); };
            bar.Controls.Add(Btn("Open folder", () => { if (Doc != null && StudyName != null) OpenPath(host.StudyFolder(Doc, StudyName)); }));
            bar.Controls.Add(Btn("Export", () => Export()));
            problems.SelectedIndexChanged += (s, e) => HighlightProblem();
            // which load cases to solve: all, or one (each case adds to the solve time)
            var which = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true };
            which.Controls.Add(new Label { Text = "Solve", AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
            which.Controls.Add(solveCase);
            which.Controls.Add(fullRun);
            var split = new SplitContainer { Dock = DockStyle.Fill, Orientation = Orientation.Horizontal, SplitterDistance = 120 };
            split.Panel1.Controls.Add(problems);
            split.Panel2.Controls.Add(logBox);
            p.Controls.Add(split);
            p.Controls.Add(activity);
            p.Controls.Add(progress);
            p.Controls.Add(which);
            p.Controls.Add(bar);
            return p;
        }

        private bool Export()
        {
            if (Doc == null || StudyName == null) { Say("Open an assembly or a part and create a study first.", true); return false; }
            try
            {
                IList<string> log;
                Say("Exporting the model...");
                Application.DoEvents();
                string path = host.Export(Doc, StudyName, out log);
                logBox.Text = string.Join(System.Environment.NewLine, log);
                Say("Exported: " + path);
                return true;
            }
            catch (Exception e) { Say("Export failed: " + e.Message, true); return false; }
        }

        private void Run(string stages, bool fast = false)
        {
            if (!Export()) return;
            string folder = host.StudyFolder(Doc, StudyName);
            problems.Items.Clear();
            // Run solves the load case chosen in "Solve" (all by default)
            string cases = stages == null && solveCase.SelectedIndex > 0 ? " --cases " + Cli.Quote((string)solveCase.SelectedItem) : "";
            string force = fullRun.Checked ? " --force" : "";
            fullRun.Checked = false;                         // a one-off: the next Run reuses again
            string mode = fast ? " --fast" : "";       // a fast run: approximate, no report (ADDENDUM-FAST-RUN-MODE)
            Cli.RunAsync(this, "run " + Cli.Quote(folder) + (stages == null ? "" : " --stages " + stages) + cases + force + mode, (r, err) =>
            {
                if (err != null) { Say(err, true); return; }
                jobId = (string)((JObj)r)["job_id"];
                jobFile = Path.Combine(folder, "run", jobId, "job.json");
                Say("Job " + jobId + " started.");
                poll.Start();
            });
        }

        private void CancelJob()
        {
            if (jobId == null || Doc == null) return;
            Cli.RunAsync(this, "cancel " + Cli.Quote(host.StudyFolder(Doc, StudyName)) + " " + jobId,
                         (r, err) => Say(err ?? "Cancel requested.", err != null));
        }

        /// <summary>Read job.json directly: it is the job's whole state, and any client may poll it.</summary>
        private int unreadable;        // polls in a row that could not read job.json

        private void PollJob()
        {
            // The job's file was fixed when it started: switching SolidWorks to a part or drawing window
            // must not stop the progress display (it froze for the rest of a run, found live).
            if (jobId == null || jobFile == null) { poll.Stop(); return; }
            try { PollJobFile(); }
            catch (Exception e) { Say("Progress display fault: " + e.Message, true); }
        }

        private void PollJobFile()
        {
            JObj job;
            try { job = (JObj)Json.Parse(File.ReadAllText(jobFile, Encoding.UTF8)); unreadable = 0; }
            catch (Exception e)
            {
                // not written yet, or mid-write: normal for a moment; say so if it lasts
                if (++unreadable == 10) Say("Waiting for the job's progress file (" + e.Message + ")", true);
                return;
            }
            string state = (string)job["state"];
            var prog = job["progress"] as JObj;
            if (prog != null && Convert.ToDouble(prog["total"]) > 0)
            {
                progress.Style = ProgressBarStyle.Continuous;
                progress.Value = (int)Math.Min(100, 100 * Convert.ToDouble(prog["done"]) / Convert.ToDouble(prog["total"]));
            }
            else progress.Style = state == "running" ? ProgressBarStyle.Marquee : ProgressBarStyle.Continuous;
            Say(state + ": " + (string)job["activity"] + "  (" + Fmt(job["elapsed_s"]) + " s)", state == "failed" || state == "blocked");
            var lines = new List<string>();
            lines.AddRange(((job["results"] as List<object>) ?? new List<object>()).Cast<string>());
            lines.AddRange(((job["verify"] as List<object>) ?? new List<object>()).Cast<string>());
            var tail = ((job["log_tail"] as List<object>) ?? new List<object>()).Cast<string>().ToList();
            if (tail.Count > 0) { lines.Add("--- log"); lines.AddRange(tail); }
            logBox.Text = string.Join(System.Environment.NewLine, lines);
            var diags = ((job["diagnostics"] as List<object>) ?? new List<object>()).Concat((job["warnings"] as List<object>) ?? new List<object>())
                        .OfType<JObj>().ToList();
            if (diags.Count != shownDiagnostics.Count)
            {
                shownDiagnostics = diags;
                problems.Items.Clear();
                foreach (var d in diags)
                    problems.Items.Add(new ListViewItem(new[] { (string)d["severity"], (string)d["code"], (string)d["what"] }) { Tag = d });
                Fit(problems);
            }
            if (state != "running" && state != "queued")
            {
                poll.Stop();
                progress.Style = ProgressBarStyle.Continuous;
                progress.Value = state == "done" ? 100 : 0;
                // what was waiting for a redraw from the current view (a plot, a report) goes ahead
                var next = afterJob;
                afterJob = null;
                replotting = false;
                if (state == "done") RefreshResults(next);
                LoadConnections();                               // a run (or Check only) rewrites connections.json
            }
        }

        private void HighlightProblem()
        {
            if (problems.SelectedItems.Count == 0 || Doc == null) return;
            var where = ((JObj)problems.SelectedItems[0].Tag)["where"] as JObj;
            if (where == null) return;
            var faces = ((where["faces"] as List<object>) ?? new List<object>()).Cast<string>().ToList();
            if (faces.Count > 0 && Study != null && host.HighlightFaces(Doc, Study, faces) > 0) return;
            var parts = ((where["parts"] as List<object>) ?? new List<object>()).Cast<string>().ToList();
            if (parts.Count > 0) host.SelectComponents(Doc, parts);
        }

        // ------------------------------------------------------------------ Results

        private TabPage ResultsTab()
        {
            var p = resTab = new TabPage("Results");
            var bar = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true };
            reportBtn = Btn("Open report", () => OpenReport(false));
            summaryBtn = Btn("Open summary", () => OpenReport(true));
            bar.Controls.Add(reportBtn);
            bar.Controls.Add(summaryBtn);
            bar.Controls.Add(Btn("Open in PrePoMax", OpenPrePoMax));
            var plots = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true };
            plots.Controls.Add(new Label { Text = "Plots:", AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
            plots.Controls.Add(Btn("Stress", () => ShowPlot("vonmises")));
            plots.Controls.Add(Btn("Displacement", () => ShowPlot("displacement")));
            plots.Controls.Add(Btn("FoS", () => ShowPlot("fos")));
            plots.Controls.Add(plotCase);
            var split = new SplitContainer { Dock = DockStyle.Fill, Orientation = Orientation.Horizontal, SplitterDistance = 160 };
            split.Panel1.Controls.Add(checksList);
            split.Panel2.Controls.Add(keyFigures);
            p.Controls.Add(split);
            p.Controls.Add(resActivity);
            p.Controls.Add(plots);
            p.Controls.Add(bar);
            p.Controls.Add(fastBanner);
            return p;
        }

        /// <summary>Read the last run's checks and key figures; then run `then` (if any).</summary>
        private void RefreshResults(Action then = null)
        {
            if (Doc == null || StudyName == null) return;
            Cli.RunAsync(this, "summary " + Cli.Quote(host.StudyFolder(Doc, StudyName)), (r, err) =>
            {
                checksList.Items.Clear();
                if (err != null) { keyFigures.Text = err; plotView = null; if (then != null) then(); return; }
                var res = (JObj)r;
                plotView = res["plot_view"] as JObj;
                bool fastResults = (string)res["run_mode"] == "fast";
                fastBanner.Visible = fastResults;
                foreach (var b in new[] { reportBtn, summaryBtn })
                {
                    if (b == null) continue;
                    b.Enabled = !fastResults;
                    tips.SetToolTip(b, fastResults ? "Reports are written from full runs only: click Run (not Fast run) first."
                                                   : (b == reportBtn ? ButtonTips["Open report"] : ButtonTips["Open summary"]));
                }
                foreach (JObj c in ((res["checks"] as List<object>) ?? new List<object>()).OfType<JObj>())
                {
                    var it = new ListViewItem(new[] { (string)c["check"], Fmt(c["value"]), Fmt(c["limit"]), (string)c["status"] });
                    if ((string)c["status"] != "PASS") it.ForeColor = Color.Firebrick;
                    checksList.Items.Add(it);
                }
                Fit(checksList);
                var sb = new StringBuilder();
                var cases = res["load_cases"] as JObj;
                // the load cases the plot buttons can show; keep the user's choice when it still exists
                // (two refreshes can overlap when tabs change quickly: rebuild only when the list changed,
                // in one step, and select by index; reading Items[0] here once threw, found live)
                var keys = cases == null ? new List<string>() : cases.Select(kv => kv.Key).ToList();
                if (!keys.SequenceEqual(plotCase.Items.Cast<object>().Select(o => o.ToString())))
                {
                    string chosen = plotCase.SelectedItem as string;
                    plotCase.BeginUpdate();
                    plotCase.Items.Clear();
                    plotCase.Items.AddRange(keys.Cast<object>().ToArray());
                    plotCase.EndUpdate();
                    int at = chosen == null ? -1 : keys.IndexOf(chosen);
                    if (keys.Count > 0) plotCase.SelectedIndex = at >= 0 ? at : 0;
                }
                else if (keys.Count > 0 && plotCase.SelectedIndex < 0) plotCase.SelectedIndex = 0;
                if (cases != null)
                    foreach (var kv in cases)
                    {
                        var lc = kv.Value as JObj;
                        if (lc == null) continue;
                        sb.AppendLine(kv.Key + ": max displacement " + Fmt(lc["max_disp"]) + " mm, reaction balance " + Fmt(lc["reaction_balance_err"]));
                        var parts = lc["parts"] as JObj;
                        if (parts != null)
                            foreach (var pk in parts)
                            {
                                var pr = pk.Value as JObj;
                                if (pr == null) continue;
                                sb.AppendLine("   " + ShortName(pk.Key) + ": membrane + bending " + Fmt(pr["membrane_bending"] ?? pr["peak_vm_excl_ties"])
                                              + " MPa (peak " + Fmt(pr["peak_vm"]) + "), min FoS " + Fmt(pr["min_fos"])
                                              + (pr["joint_stress"] != null ? "; joint " + Fmt(pr["joint_stress"]) + " MPa (FoS " + Fmt(pr["joint_fos"]) + ")" : ""));
                            }
                        // bonded joints, by structural stress (D33): the worst few
                        var joints = (lc["joints"] as List<object> ?? new List<object>()).OfType<JObj>()
                                     .OrderByDescending(j => Convert.ToDouble(j["structural_stress"] ?? 0.0)).Take(3).ToList();
                        foreach (var j in joints)
                            sb.AppendLine("   joint " + ShortName((string)j["part"]) + " -> " + ShortName((string)j["other"]) + ": structural stress "
                                          + Fmt(j["structural_stress"]) + " MPa (FoS " + Fmt(j["fos"]) + ")");
                    }
                keyFigures.Text = (fastResults ? "APPROXIMATE (fast run): values are indicative only." + System.Environment.NewLine + System.Environment.NewLine : "") + sb.ToString();
                if (then != null) then();
            });
        }

        // ------------------------------------------------------------------ plots follow the SolidWorks view

        private JObj plotView;          // the view the last run's plots were drawn from (results.json plot_view)
        private bool replotting;        // a redraw job is running
        private Action afterJob;        // what to do when it finishes (show a plot, open a report)
        private const double SAME_VIEW_COS = 0.99985;   // cos 1 degree: closer than this is the same view

        /// <summary>
        /// Read the results; if the plots were drawn from a different view than the one now on screen in
        /// SolidWorks, redraw them (plots, report and summary; no re-solve) first. Then run `then`.
        /// </summary>
        private void WithCurrentView(Action then)
        {
            if (Doc == null || StudyName == null) return;
            if (replotting) { if (then != null) afterJob = then; return; }   // already redrawing: do it after
            RefreshResults(() =>
            {
                var v = host.CurrentView(Doc);
                if (plotView == null || v == null || SameView(v, plotView)) { if (then != null) then(); return; }
                Replot(v, then);
            });
        }

        /// <summary>A vector as doubles: the add-in's own view is a double[], JSON from the CLI a list.</summary>
        private static double[] Doubles(object v)
        {
            var d = v as double[];
            if (d != null) return d;
            var l = v as IEnumerable<object>;
            return l == null ? new double[0] : l.Select(x => Convert.ToDouble(x, CultureInfo.InvariantCulture)).ToArray();
        }

        private static bool SameView(JObj a, JObj b)
        {
            Func<JObj, string, double[]> vec = (o, k) => Doubles(o[k]);
            Func<double[], double[], double> cos = (x, y) =>
            {
                if (x.Length != 3 || y.Length != 3) return -1.0;   // unreadable: redraw rather than keep stale plots
                double d = x[0] * y[0] + x[1] * y[1] + x[2] * y[2];
                double n = Math.Sqrt((x[0] * x[0] + x[1] * x[1] + x[2] * x[2]) * (y[0] * y[0] + y[1] * y[1] + y[2] * y[2]));
                return n > 0 ? d / n : 1.0;
            };
            return cos(vec(a, "toward_viewer"), vec(b, "toward_viewer")) >= SAME_VIEW_COS
                && cos(vec(a, "up"), vec(b, "up")) >= SAME_VIEW_COS;
        }

        /// <summary>Redraw the plots and reports from the view on screen (a post + report job, about 10-30 s).</summary>
        private void Replot(JObj v, Action then)
        {
            if (poll.Enabled) { if (then != null) then(); return; }   // a run is going: show what there is
            Func<object, string> xyz = o => string.Join(",", Doubles(o).Select(x => x.ToString("R", CultureInfo.InvariantCulture)));
            string folder = host.StudyFolder(Doc, StudyName);
            replotting = true;
            afterJob = then;
            Say("Updating the plots to the current SolidWorks view...");
            Cli.RunAsync(this, "replot " + Cli.Quote(folder) + " --toward=" + xyz(v["toward_viewer"]) + " --up=" + xyz(v["up"]), (r, err) =>
            {
                if (err != null) { replotting = false; afterJob = null; Say(err, true); if (then != null) then(); return; }
                jobId = (string)((JObj)r)["job_id"];
                jobFile = Path.Combine(folder, "run", jobId, "job.json");
                poll.Start();
            });
        }

        /// <summary>A plot of the last run in its own window (not modal: several can stay open), drawn
        /// from the view now on screen in SolidWorks.</summary>
        private void ShowPlot(string kind)
        {
            WithCurrentView(() => ShowPlotNow(kind));
        }

        private void ShowPlotNow(string kind, bool redrawn = false)
        {
            if (Doc == null || StudyName == null) return;
            string folder = host.StudyFolder(Doc, StudyName);
            string lc = plotCase.SelectedItem as string;
            if (lc == null) { Say("No results yet: run the study first.", true); return; }
            // the 3-D data is written with the plots; results from before it existed are redrawn once
            if (!File.Exists(Path.Combine(folder, "results", lc.Replace(" ", "_") + "_fields.vtp")))
            {
                var v = host.CurrentView(Doc);
                if (redrawn || v == null) { Say("No 3-D result data for " + lc + ": run the study again.", true); return; }
                Replot(v, () => ShowPlotNow(kind, true));
                return;
            }
            // the viewer is started by a background process: let it come to the front
            AllowSetForegroundWindow(ASFW_ANY);
            Say("Opening the " + FieldTitle(kind) + " of " + lc + " in a 3-D window...");
            Cli.RunAsync(this, "view " + Cli.Quote(folder) + " --case " + Cli.Quote(lc) + " --field " + kind,
                         (r, err) => Say(err ?? "Opened the " + FieldTitle(kind) + " of " + lc + ": left drag to rotate, wheel to zoom, right drag to pan, Reset view to return.", err != null));
        }

        private static string FieldTitle(string kind)
        {
            return kind == "vonmises" ? "von Mises stress" : kind == "fos" ? "factor of safety" : kind;
        }

        private const int ASFW_ANY = -1;
        [System.Runtime.InteropServices.DllImport("user32.dll")]
        private static extern bool AllowSetForegroundWindow(int processId);

        private const string PREPOMAX_DOWNLOADS = "https://prepomax.fs.um.si/downloads/";

        /// <summary>The last run in PrePoMax; when PrePoMax is not set up, offer to find it or download it.</summary>
        private void OpenPrePoMax()
        {
            if (Doc == null || StudyName == null) return;
            AllowSetForegroundWindow(ASFW_ANY);
            Cli.RunAsync(this, "prepomax " + Cli.Quote(host.StudyFolder(Doc, StudyName)), (r, err) =>
            {
                if (err == null) return;
                // shown where the user is looking (the Results tab), not on the Run tab's status line
                if (err.Contains("not configured")) SetUpPrePoMax();
                else MessageBox.Show(this, "PrePoMax could not be opened:\n" + err, "AsmFEA");
            });
        }

        private void SetUpPrePoMax()
        {
            string choice = Choose3("PrePoMax", "PrePoMax is a free program for looking at results in detail. It is not set up yet.\n\n"
                                    + "If it is on this computer, find PrePoMax.exe; otherwise download it (a zip file: unzip it anywhere, then come back here).",
                                    "Find PrePoMax.exe...", "Download PrePoMax", "Cancel");
            if (choice == "Download PrePoMax") { OpenPath(PREPOMAX_DOWNLOADS); return; }
            if (choice != "Find PrePoMax.exe...") return;
            using (var dlg = new OpenFileDialog { Title = "Find PrePoMax.exe", Filter = "PrePoMax|PrePoMax.exe|Programs (*.exe)|*.exe", CheckFileExists = true })
            {
                if (dlg.ShowDialog(this) != DialogResult.OK) return;
                Cli.RunAsync(this, "config --prepomax " + Cli.Quote(dlg.FileName), (r, err) =>
                {
                    if (err != null) { MessageBox.Show(this, "PrePoMax could not be set up:\n" + err, "AsmFEA"); return; }
                    Say("PrePoMax set up: " + dlg.FileName);
                    OpenPrePoMax();
                });
            }
        }

        /// <summary>A message with three buttons; returns the chosen button's text (null when closed).</summary>
        private string Choose3(string title, string text, string a, string b, string c)
        {
            using (var f = new Form { Text = "AsmFEA: " + title, Width = 460, Height = 210, FormBorderStyle = FormBorderStyle.FixedDialog,
                                      StartPosition = FormStartPosition.CenterParent, MinimizeBox = false, MaximizeBox = false, ShowIcon = false })
            {
                string chosen = null;
                var label = new Label { Text = text, Left = 12, Top = 12, Width = 420, Height = 100 };
                var bar = new FlowLayoutPanel { Dock = DockStyle.Bottom, AutoSize = true, FlowDirection = FlowDirection.RightToLeft, Padding = new Padding(8) };
                foreach (var t in new[] { c, b, a })
                {
                    var btn = new Button { Text = t, AutoSize = true };
                    btn.Click += (s, e) => { chosen = t; f.Close(); };
                    bar.Controls.Add(btn);
                }
                f.Controls.Add(label);
                f.Controls.Add(bar);
                f.ShowDialog(this);
                return chosen;
            }
        }

        /// <summary>The full report, or the half-page summary for risk assessments.</summary>
        private void OpenReport(bool summary)
        {
            WithCurrentView(() => OpenReportNow(summary));       // its plots follow the view on screen
        }

        private void OpenReportNow(bool summary)
        {
            if (Doc == null || StudyName == null) return;
            Cli.RunAsync(this, "report " + Cli.Quote(host.StudyFolder(Doc, StudyName)) + (summary ? " --summary" : ""), (r, err) =>
            {
                if (err != null) { MessageBox.Show(this, "The report could not be opened:\n" + err, "AsmFEA"); return; }
                var path = r as string ?? (string)((r as JObj)?["path"] ?? (r as JObj)?["report"]);
                if (path != null) OpenPath(path); else MessageBox.Show(this, "No report path was returned.", "AsmFEA");
            });
        }

        private static void OpenPath(string path)
        {
            try { Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); }
            catch (Exception) { }
        }

        // ------------------------------------------------------------------ help

        /// <summary>A diagnostic in full: what, the plain-English explanation, where, evidence, causes, actions.</summary>
        private void ShowHelp(JObj d)
        {
            var sb = new StringBuilder();
            string sev = (string)d["severity"];
            sb.AppendLine((sev == "blocking" ? "Error" : sev == "warning" ? "Warning" : "Note") + " " + (string)d["code"] + " (" + (string)d["stage"] + ")");
            sb.AppendLine();
            sb.AppendLine((string)d["what"]);
            if (!string.IsNullOrEmpty((string)d["explain"])) { sb.AppendLine(); sb.AppendLine((string)d["explain"]); }
            var where = d["where"] as JObj;
            if (where != null)
            {
                var w = where.Where(kv => kv.Value is List<object> && ((List<object>)kv.Value).Count > 0).ToList();
                if (w.Count > 0)
                {
                    sb.AppendLine(); sb.AppendLine("Where:");
                    foreach (var kv in w) sb.AppendLine("  " + kv.Key + ": " + Fmt(kv.Value));
                }
            }
            Bullets(sb, "Likely causes:", d["likely_causes"] as List<object>);
            Bullets(sb, "What to do:", d["actions"] as List<object>);
            var ev = d["evidence"] as JObj;
            if (ev != null && ev.Any())
            {
                sb.AppendLine(); sb.AppendLine("Evidence:");
                foreach (var kv in ev) sb.AppendLine("  " + kv.Key + ": " + Json.Write(kv.Value).Trim());
            }
            if (!string.IsNullOrEmpty((string)d["raw"])) { sb.AppendLine(); sb.AppendLine("Raw output:"); sb.AppendLine((string)d["raw"]); }
            ShowText((string)d["code"], sb.ToString().TrimEnd());
        }

        private static void Bullets(StringBuilder sb, string heading, List<object> items)
        {
            if (items == null || items.Count == 0) return;
            sb.AppendLine(); sb.AppendLine(heading);
            foreach (var i in items)
            {
                var o = i as JObj;
                sb.AppendLine("  - " + (o != null ? (string)o["text"] ?? Json.Write(o).Trim() : Fmt(i)));
            }
        }

        private void ShowText(string title, string text)
        {
            using (var f = new Form { Text = "AsmFEA: " + title, Width = 560, Height = 440, StartPosition = FormStartPosition.CenterParent, MinimizeBox = false, ShowIcon = false })
            {
                var tb = new TextBox
                {
                    Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, WordWrap = true,
                    Font = new Font("Segoe UI", 9.5f), BackColor = SystemColors.Window,
                    Text = text.Replace("\r\n", "\n").Replace("\n", System.Environment.NewLine),
                };
                var close = new Button { Text = "Close", DialogResult = DialogResult.Cancel, Dock = DockStyle.Bottom };
                f.Controls.Add(tb);
                f.Controls.Add(close);
                f.CancelButton = close;
                f.Shown += (s, e) => { tb.SelectionStart = 0; tb.SelectionLength = 0; close.Focus(); };
                f.ShowDialog(this);
            }
        }

        // ------------------------------------------------------------------ small dialogs

        private static string Choose(string title, string text, string[] options)
        {
            using (var f = new Form { Text = title, Width = 320, Height = 140, FormBorderStyle = FormBorderStyle.FixedDialog, StartPosition = FormStartPosition.CenterParent, MinimizeBox = false, MaximizeBox = false })
            {
                var cb = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Left = 10, Top = 30, Width = 120 };
                cb.Items.AddRange(options.Cast<object>().ToArray());
                cb.SelectedIndex = 0;
                f.Controls.Add(new Label { Text = text, Left = 10, Top = 8, AutoSize = true });
                f.Controls.Add(cb);
                var ok = new Button { Text = "OK", DialogResult = DialogResult.OK, Left = 130, Top = 62 };
                var cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel, Left = 212, Top = 62 };
                f.Controls.Add(ok);
                f.Controls.Add(cancel);
                f.AcceptButton = ok;
                f.CancelButton = cancel;
                return f.ShowDialog() == DialogResult.OK ? (string)cb.SelectedItem : null;
            }
        }

        private static string Prompt(string title, string text, string value)
        {
            using (var f = new Form { Text = title, Width = 320, Height = 140, FormBorderStyle = FormBorderStyle.FixedDialog, StartPosition = FormStartPosition.CenterParent, MinimizeBox = false, MaximizeBox = false })
            {
                var tb = new TextBox { Text = value, Left = 10, Top = 30, Width = 280 };
                f.Controls.Add(new Label { Text = text, Left = 10, Top = 8, AutoSize = true });
                f.Controls.Add(tb);
                var ok = new Button { Text = "OK", DialogResult = DialogResult.OK, Left = 130, Top = 62 };
                var cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel, Left = 212, Top = 62 };
                f.Controls.Add(ok);
                f.Controls.Add(cancel);
                f.AcceptButton = ok;
                f.CancelButton = cancel;
                return f.ShowDialog() == DialogResult.OK ? tb.Text : null;
            }
        }
    }

    /// <summary>
    /// The pane's tab strip, drawn by hand so the tabs stand out from SolidWorks' grey: the
    /// selected tab is solid accent blue (the guide's colour) joined to a bar along the top of the
    /// page; the others are white with an outline, and tint under the mouse.
    /// </summary>
    public class PaneTabs : TabControl
    {
        private static readonly Color Accent = Color.FromArgb(0x1f, 0x6f, 0xd1), Strip = Color.FromArgb(0xe4, 0xea, 0xf2),
                                      Idle = Color.FromArgb(0xfb, 0xfc, 0xfd), Hot = Color.FromArgb(0xdc, 0xe8, 0xf8),
                                      Line = Color.FromArgb(0xb8, 0xc4, 0xd4), Ink = Color.FromArgb(0x1c, 0x24, 0x30);
        private int hot = -1;

        public PaneTabs()
        {
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer
                     | ControlStyles.ResizeRedraw, true);
            Padding = new Point(5, 5);                       // taller tabs; as narrow as before, so all five fit the pane
        }

        private static System.Drawing.Drawing2D.GraphicsPath TopRounded(Rectangle r, int rad)
        {
            var p = new System.Drawing.Drawing2D.GraphicsPath();
            p.AddLine(r.Left, r.Bottom, r.Left, r.Top + rad);
            p.AddArc(r.Left, r.Top, 2 * rad, 2 * rad, 180, 90);
            p.AddArc(r.Right - 2 * rad, r.Top, 2 * rad, 2 * rad, 270, 90);
            p.AddLine(r.Right, r.Top + rad, r.Right, r.Bottom);
            p.CloseFigure();
            return p;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.Clear(Strip);
            if (TabCount == 0) return;
            // the page sits at DisplayRectangle: the accent bar goes just above it, the tabs stand on the bar
            int pageTop = DisplayRectangle.Top, barTop = pageTop - 4;
            using (var pen = new Pen(Line)) g.DrawRectangle(pen, DisplayRectangle.Left - 1, barTop, DisplayRectangle.Width + 1, Height - barTop - 1);
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            for (int i = 0; i < TabCount; i++)
                if (i != SelectedIndex) DrawTab(g, i, barTop, false);
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.None;
            using (var bar = new SolidBrush(Accent)) g.FillRectangle(bar, DisplayRectangle.Left - 1, barTop, DisplayRectangle.Width + 2, 4);
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            if (SelectedIndex >= 0) DrawTab(g, SelectedIndex, barTop, true);
        }

        private void DrawTab(Graphics g, int i, int barTop, bool selected)
        {
            var t = GetTabRect(i);
            int top = selected ? t.Y - 1 : t.Y + 2;
            var r = new Rectangle(t.X + 1, top, t.Width - 2, (selected ? barTop + 1 : barTop) - top);
            using (var path = TopRounded(r, 4))
            {
                using (var fill = new SolidBrush(selected ? Accent : i == hot ? Hot : Idle)) g.FillPath(fill, path);
                if (!selected) using (var pen = new Pen(Line)) g.DrawPath(pen, path);
            }
            using (var bold = selected ? new Font(Font, FontStyle.Bold) : null)
                TextRenderer.DrawText(g, TabPages[i].Text, bold ?? Font, r, selected ? Color.White : Ink,
                    TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine
                    | TextFormatFlags.NoPrefix | TextFormatFlags.NoClipping);
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            int over = -1;
            for (int i = 0; i < TabCount; i++)
                if (GetTabRect(i).Contains(e.Location)) over = i;
            if (over != hot) { hot = over; Invalidate(); }
        }

        protected override void OnMouseLeave(EventArgs e)
        {
            base.OnMouseLeave(e);
            if (hot != -1) { hot = -1; Invalidate(); }
        }
    }

    /// <summary>
    /// A load's case and values, for a new load or to edit one: force and gravity as X, Y, Z
    /// components (assembly axes) with the resultant shown as you type; a force also takes where it
    /// acts, as X, Y, Z distances from the centre of its faces (non-zero makes it a remote force);
    /// pressure takes one value.
    /// </summary>
    public class LoadDialog : Form
    {
        private readonly string type;
        private readonly TextBox magnitude = new TextBox { Width = 80 };
        private readonly TextBox[] comp = Boxes(), offset = Boxes();
        private readonly Label resultant = new Label { AutoSize = true, Padding = new Padding(0, 6, 0, 0) };
        private readonly ToolTip tips = new ToolTip { AutoPopDelay = 20000, InitialDelay = 400 };


        private static TextBox[] Boxes()
        {
            return Enumerable.Range(0, 3).Select(i => new TextBox { Width = 70 }).ToArray();
        }

        private static string G(double v) { return Math.Round(v, 9).ToString("G6", CultureInfo.InvariantCulture); }

        private static double? Parse(string s)
        {
            if (string.IsNullOrWhiteSpace(s)) return 0;               // a blank component is zero
            double v;
            return double.TryParse(s.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out v) ? v : (double?)null;
        }

        private static double[] Read(TextBox[] boxes)
        {
            var v = boxes.Select(b => Parse(b.Text)).ToArray();
            return v.Any(x => x == null) ? null : v.Select(x => x.Value).ToArray();
        }

        private static FlowLayoutPanel Xyz(TextBox[] boxes)
        {
            var row = new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = new Padding(0) };
            for (int i = 0; i < 3; i++)
            {
                row.Controls.Add(new Label { Text = "XYZ"[i].ToString(), AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
                row.Controls.Add(boxes[i]);
            }
            return row;
        }

        private static List<double> Vec(object v)
        {
            var l = v as List<object>;
            return l == null || l.Count != 3 ? null : l.Select(x => Convert.ToDouble(x, CultureInfo.InvariantCulture)).ToList();
        }

        public LoadDialog(string type, string loadCase, JObj existing)
        {
            this.type = type;
            bool isForce = type == "force" || type == "remote_force";
            string name = isForce ? "force" : type;
            Text = (existing == null ? "Add " : "Edit " + existing["id"] + ": ") + name + " (load case " + loadCase + ")";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterParent;
            MinimizeBox = MaximizeBox = false;

            // starting values: the load being edited, else a typical one
            double m = existing?["magnitude"] == null ? 0 : Convert.ToDouble(existing["magnitude"], CultureInfo.InvariantCulture);
            var d = existing == null ? null : Vec(existing["direction"]);
            var start = d != null ? d.Select(x => m * x).ToArray()
                      : new[] { 0.0, type == "gravity" ? -9810.0 : -1000.0, 0.0 };
            for (int i = 0; i < 3; i++) comp[i].Text = G(start[i]);
            var off = existing == null ? null : Vec(existing["offset"]);
            for (int i = 0; i < 3; i++) offset[i].Text = G(off == null ? 0 : off[i]);
            magnitude.Text = existing != null ? G(m) : "0.1";

            var t = new TableLayoutPanel { ColumnCount = 2, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Location = new Point(0, 0), Padding = new Padding(6) };
            Action<string, Control> add = (label, c) =>
            {
                t.Controls.Add(new Label { Text = label, AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
                t.Controls.Add(c);
            };
            if (type == "pressure")
            {
                add("Pressure, MPa", magnitude);
                tips.SetToolTip(magnitude, "Positive pushes into the faces; negative pulls them out.");
            }
            else
            {
                string unit = type == "gravity" ? "mm/s²" : "N";
                add(type == "gravity" ? "Acceleration, mm/s²" : "Force, N", Xyz(comp));
                add("Resultant", resultant);
                foreach (var b in comp)
                    tips.SetToolTip(b, type == "gravity"
                        ? "Acceleration components along the assembly X, Y, Z axes (Earth's gravity is 9810 mm/s², usually -Y)."
                        : "Force components along the assembly X, Y, Z axes; the total is shared over all the selected faces.");
                tips.SetToolTip(resultant, "The size of the resultant of the three components, in " + unit + ".");
                foreach (var b in comp) b.TextChanged += (s, e) => ShowResultant(unit);
                ShowResultant(unit);
            }
            if (isForce)
            {
                add("Acts at, mm from the faces' centre", Xyz(offset));
                foreach (var b in offset)
                    tips.SetToolTip(b, "Where the force acts: X, Y, Z distances (assembly axes) from the area-weighted centre of the selected faces; "
                                     + "0, 0, 0 = on the faces. Off the faces, they carry the force and its moment: e.g. a sideways distance with a force across it gives a torque.");
                if (existing?["point"] != null)
                    add("", new Label { Text = "(This load was given as a fixed point; OK replaces it with these distances.)", AutoSize = true });
            }

            var buttons = new FlowLayoutPanel { AutoSize = true };
            var ok = new Button { Text = "OK", DialogResult = DialogResult.None };
            var cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel };
            ok.Click += (s, e) => { if (Check()) DialogResult = DialogResult.OK; };
            buttons.Controls.Add(ok);
            buttons.Controls.Add(cancel);
            add("", buttons);
            Controls.Add(t);
            // size the dialog to its contents once laid out (an AutoSize form does not grow for a docked table)
            Load += (s, e) => ClientSize = new Size(t.PreferredSize.Width + 6, t.PreferredSize.Height + 6);
            AcceptButton = ok;
            CancelButton = cancel;
        }

        private void ShowResultant(string unit)
        {
            var v = Read(comp);
            resultant.Text = v == null ? "(not a number)" : G(Math.Sqrt(v.Sum(x => x * x))) + " " + unit;
        }

        private bool Check()
        {
            string problem = null;
            if (type == "pressure")
            {
                var p = Parse(magnitude.Text);
                if (p == null || p == 0) problem = "Give a non-zero pressure.";
            }
            else
            {
                var v = Read(comp);
                if (v == null) problem = "Each component must be a number (blank counts as zero).";
                else if (Math.Sqrt(v.Sum(x => x * x)) == 0) problem = "At least one component must be non-zero.";
                else if ((type == "force" || type == "remote_force") && Read(offset) == null) problem = "Each distance must be a number (blank counts as zero).";
            }
            if (problem != null) MessageBox.Show(this, problem, "AsmFEA");
            return problem == null;
        }

        /// <summary>Store the values in the load: magnitude = the resultant, direction = its unit vector.</summary>
        public void WriteTo(JObj ld)
        {
            if (type == "pressure")
            {
                ld["magnitude"] = Parse(magnitude.Text).Value;
                ld.Remove("direction");
                return;
            }
            var v = Read(comp);
            double r = Math.Sqrt(v.Sum(x => x * x));
            ld["magnitude"] = r;
            ld["direction"] = v.Select(x => (object)(x / r)).ToList();
            if (type == "force" || type == "remote_force")
            {
                // a zero offset is a plain force (the same answer, D27): the report then states no
                // remote-force assumption and the arrow has no point marker
                var off = Read(offset);
                ld.Remove("point");
                if (off.All(x => x == 0)) { ld["type"] = "force"; ld.Remove("offset"); }
                else { ld["type"] = "remote_force"; ld["offset"] = off.Select(x => (object)x).ToList(); }
            }
        }
    }

    /// <summary>Edit a fixture: its type and, for pinned, which movements and turnings are held.</summary>
    public class FixtureDialog : Form
    {
        private readonly ComboBox typeBox = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 110 };
        private readonly CheckBox[] move = Checks(), turn = Checks();
        private readonly ToolTip tips = new ToolTip { AutoPopDelay = 20000, InitialDelay = 400 };

        private static CheckBox[] Checks()
        {
            return Enumerable.Range(0, 3).Select(i => new CheckBox { Text = "XYZ"[i].ToString(), AutoSize = true }).ToArray();
        }

        private static HashSet<int> Axes(object v)
        {
            var l = v as List<object>;
            return l == null ? null : new HashSet<int>(l.Select(x => Convert.ToInt32(x, CultureInfo.InvariantCulture)));
        }

        public FixtureDialog(JObj fx)
        {
            Text = "Edit " + fx["id"] + ": fixture";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterParent;
            MinimizeBox = MaximizeBox = false;
            string type = (string)fx["type"];
            typeBox.Items.AddRange(new object[] { "fixed", "pinned" });
            if (type == "displacement") typeBox.Items.Add("displacement");
            typeBox.SelectedItem = type;
            var dofs = Axes(fx["dofs"]);
            var rots = Axes(fx["hold_rotations"]) ?? new HashSet<int>();
            for (int i = 0; i < 3; i++)
            {
                move[i].Checked = dofs == null || dofs.Contains(i + 1);    // pinned holds all three by default
                turn[i].Checked = rots.Contains(i + 1);
            }

            var t = new TableLayoutPanel { ColumnCount = 2, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Location = new Point(0, 0), Padding = new Padding(6) };
            Action<string, Control> add = (label, c) =>
            {
                t.Controls.Add(new Label { Text = label, AutoSize = true, Padding = new Padding(0, 6, 0, 0) });
                t.Controls.Add(c);
            };
            Func<CheckBox[], FlowLayoutPanel> row = boxes =>
            {
                var p = new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = new Padding(0) };
                p.Controls.AddRange(boxes);
                return p;
            };
            add("Type", typeBox);
            add("Hold movement along", row(move));
            add("Hold turning about", row(turn));
            tips.SetToolTip(typeBox, "fixed: the faces cannot move or turn at all. pinned: the faces' centroid is held in the ticked directions and the faces turn freely, except about the ticked axes.");
            foreach (var b in move) tips.SetToolTip(b, "Pinned: hold the faces' centroid against movement along this assembly axis.");
            foreach (var b in turn) tips.SetToolTip(b, "Pinned: also stop the faces turning about this assembly axis (e.g. Z stops a pin twisting about Z).");
            typeBox.SelectedIndexChanged += (s, e) => Enable();
            Enable();

            var buttons = new FlowLayoutPanel { AutoSize = true };
            var ok = new Button { Text = "OK", DialogResult = DialogResult.None };
            var cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel };
            ok.Click += (s, e) =>
            {
                string tp = (string)typeBox.SelectedItem;
                if (tp != "fixed" && !move.Any(b => b.Checked) && !(tp == "pinned" && turn.Any(b => b.Checked)))
                {
                    MessageBox.Show(this, "Tick at least one direction to hold.", "AsmFEA");
                    return;
                }
                DialogResult = DialogResult.OK;
            };
            buttons.Controls.Add(ok);
            buttons.Controls.Add(cancel);
            add("", buttons);
            Controls.Add(t);
            // size the dialog to its contents once laid out (an AutoSize form does not grow for a docked table)
            Load += (s, e) => ClientSize = new Size(t.PreferredSize.Width + 6, t.PreferredSize.Height + 6);
            AcceptButton = ok;
            CancelButton = cancel;
        }

        private void Enable()
        {
            string tp = (string)typeBox.SelectedItem;
            foreach (var b in move) b.Enabled = tp != "fixed";
            foreach (var b in turn) b.Enabled = tp == "pinned";
        }

        public void WriteTo(JObj fx)
        {
            string tp = (string)typeBox.SelectedItem;
            fx["type"] = tp;
            fx.Remove("dofs");
            fx.Remove("hold_rotations");
            Func<CheckBox[], List<object>> ticked = boxes => Enumerable.Range(0, 3).Where(i => boxes[i].Checked).Select(i => (object)(i + 1)).ToList();
            if (tp == "fixed") return;
            var dofs = ticked(move);
            if (tp == "displacement" || dofs.Count < 3) fx["dofs"] = dofs;   // pinned holds all three when dofs is absent
            if (tp == "pinned" && ticked(turn).Count > 0) fx["hold_rotations"] = ticked(turn);
        }
    }
}
