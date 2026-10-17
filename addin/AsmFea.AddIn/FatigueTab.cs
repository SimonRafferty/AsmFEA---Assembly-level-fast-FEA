// The Fatigue tab (release 2 F5, D108): study.fatigue edited in the pane, Assess fatigue (the fatigue stage only, no
// new solve), the key figures, and the fatigue report and summary. The data handling is FatigueSettings
// (AsmFea.Extract, tested without SolidWorks); this file is the controls. LIVE-T17..T19 (addin/LIVE-TESTS.md).
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Windows.Forms;
using AsmFea.Extract;

namespace AsmFea.AddIn
{
    public partial class StudyPane
    {
        private readonly ComboBox fatStandard = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 190 };
        private readonly ComboBox fatMean = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 100 };
        private readonly ComboBox fatWeld = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 290 };
        private readonly ComboBox fatStruct = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 70 };
        private readonly ComboBox fatThread = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 100 };
        private readonly TextBox fatBlocks = new TextBox { Width = 70 }, fatGammaF = new TextBox { Width = 50 }, fatGammaM = new TextBox { Width = 50 };
        private readonly DataGridView fatSpectrum = Grid(), fatMaterials = Grid(), fatJoints = Grid();
        private readonly Label fatKey = new Label { AutoSize = false, Height = 40, Dock = DockStyle.Top, Padding = new Padding(4),
                                                    BackColor = System.Drawing.Color.FromArgb(0xee, 0xf3, 0xf9) };
        private readonly ColourBar fatProgress = new ColourBar { Dock = DockStyle.Top, Height = 14, Margin = new Padding(3, 2, 3, 2) };
        private readonly Label fatActivity = new Label { AutoSize = false, Dock = DockStyle.Top, Height = 0, AutoEllipsis = true,
                                                         ForeColor = System.Drawing.Color.DimGray, Padding = new Padding(2, 2, 0, 2) };
        private readonly Label fatProblems = new Label { AutoSize = false, Dock = DockStyle.Top, Height = 0, ForeColor = System.Drawing.Color.Firebrick,
                                                         Padding = new Padding(4, 2, 0, 2) };

        private static DataGridView Grid()
        {
            return new DataGridView
            {
                BackgroundColor = System.Drawing.SystemColors.Window, BorderStyle = BorderStyle.FixedSingle,
                Height = 130, Dock = DockStyle.Top, AllowUserToAddRows = true, AllowUserToDeleteRows = true, RowHeadersWidth = 24,
                AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill, EditMode = DataGridViewEditMode.EditOnEnter,
            };
        }

        // The tab's tool tips: most users will not have done a fatigue assessment before, so each says what the setting
        // means as well as what it does (the user's request, 2026-10-17). "\n" breaks a line; WrapTip wraps the rest.
        private static readonly Dictionary<string, string> FatTips = new Dictionary<string, string>
        {
            { "assess", "Work out the fatigue damage from the last static results. There is no new solve.\n\n"
                        + "Fatigue is cracking under repeated loading, at stresses well below yield. For every part, bonded joint "
                        + "(weld), joint wall and preloaded bolt, AsmFEA turns the stresses of the load cases into stress cycles "
                        + "(the spectrum below). It then adds up the damage each cycle does, from the material's S-N curve "
                        + "(Miner's rule).\n\nDamage 1 means a crack is expected within the design life." },
            { "cancel", "Stop the running fatigue assessment." },
            { "report", "Open the fatigue report: the result and damage plot, the weakest item of each kind (part, weld, "
                        + "wall, bolt), the spectrum, what to note and the assumptions." },
            { "summary", "Open the half-page fatigue summary: the weakest item's figures beside the damage plot." },
            { "data", "Open the fatigue data report: every item ranked, the S-N curves and where they come from, what was "
                      + "not assessed, and every warning in full." },
            { "on", "Include a fatigue assessment in this study.\n\n"
                    + "Fatigue matters for anything loaded many times: lifting, travelling, vibrating or rotating parts. "
                    + "Even a rig that only has to last a few hours can see thousands of cycles.\n\n"
                    + "It uses the static results, so run the static study first." },
            { "standard", "The design code whose S-N curves are used for welds and bolts.\n\n"
                          + "BS 7608: the UK standard for steel structures (the default).\n"
                          + "PD 5500 Annex C: pressure vessels.\n"
                          + "EN 1993-1-9: the Eurocode for steel structures.\n"
                          + "EN 13001-3-1: cranes." },
            { "blocks", "How many times the whole spectrum is repeated in the design life.\n\n"
                        + "Example: if the spectrum is one day's use and the rig must last 200 days, enter 200." },
            { "spectrum", "The loading history. Each row is a load that cycles between a maximum and a minimum a number of "
                          + "times.\n\n"
                          + "Max % and Min % are percentages of the load case:\n"
                          + "  100 and 0: the load is applied in full and removed.\n"
                          + "  100 and -100: the load fully reverses.\n"
                          + "  100 and 80: a small ripple on a steady load (little damage).\n\n"
                          + "Usually both ends use the same load case (a load put on and taken off).\n"
                          + "Pick a different load case for the minimum only for a load that moves between two positions "
                          + "(e.g. lifted at one end then the other, or a trolley travelling along a beam): at every point the "
                          + "stress then goes from one case's state to the other's, which can reverse the stress in between.\n\n"
                          + "Cycles: how many times the row happens in one pass of the spectrum." },
            { "col:case", "The load case at the top of the cycle." },
            { "col:max", "The load at the top of the cycle, as a % of the load case.\n100 = the load case as defined." },
            { "col:mincase", "The load case at the bottom of the cycle. Normally the same as the maximum's.\n"
                             + "A different one only for a load that moves between two positions: each point then cycles "
                             + "between the two cases' stresses." },
            { "col:min", "The load at the bottom of the cycle, as a % of its load case.\n0 = no load; negative = reversed." },
            { "col:cycles", "How many times this cycle happens in one pass of the spectrum (e.g. 5000 or 5e3)." },
            { "materials", "The S-N curve of each material: the stress it can take for a given number of cycles before it "
                           + "cracks. Used for the parent material away from welds; welds use the standard's weld curves.\n\n"
                           + "Built in: the SolidWorks material's own S-N curve if it has one; otherwise AsmFEA's data, "
                           + "measured where published and otherwise ESTIMATED from the tensile strength." },
            { "col:kind", "Where the S-N curve comes from:\n"
                          + "  Built in: SolidWorks' or AsmFEA's data (shown greyed).\n"
                          + "  Basquin fit: your own test data, as Sf' and b.\n"
                          + "  Class of the standard: a class for unwelded material (e.g. BS 7608 class B)." },
            { "col:sf", "Sf' (fatigue strength coefficient, MPa): the curve's stress amplitude at one reversal.\n"
                        + "With b it defines the curve: amplitude = Sf' x (2N)^b." },
            { "col:b", "b (fatigue strength exponent): the slope of the curve on log-log axes.\n"
                       + "Negative; typically -0.05 to -0.12 for metals." },
            { "col:cls", "The standard's class for unwelded material, used instead of an S-N curve." },
            { "col:surface", "The surface finish, for the built-in data: rougher surfaces crack sooner.\n"
                             + "Blank: hot rolled for steel, as made for other metals." },
            { "col:source", "Where the data comes from: your reference, or the built-in source (hover over a built-in "
                            + "row's cell for the full reference)." },
            { "mean", "How the mean (average) stress of a cycle reduces the allowed range, for your own S-N curves.\n\n"
                      + "Goodman: the usual, cautious choice.\nGerber: less cautious.\nNone: the mean stress ignored.\n\n"
                      + "The standards' weld curves already allow for it." },
            { "gammaF", "Partial factor on stress: every stress range is multiplied by it.\n"
                        + "1 = none. Use more than 1 where the loads are uncertain." },
            { "gammaM", "Partial factor on strength: the S-N curve's strength is divided by it.\n"
                        + "1 = none. The standards give values by how critical the part is and how easily it can be inspected." },
            { "weld", "How the bonded joints (welds) are assessed.\n\n"
                      + "Structural stress (Dong): reads the stress along the joint line from the model and uses one curve "
                      + "for every weld, so no category is needed. Recommended.\n\n"
                      + "Nominal stress: the beam-theory stress across the joint, with the standard's detail category, "
                      + "which you give per joint below." },
            { "struct", "The weld class used with the structural stress method.\n"
                        + "BS 7608 class D (EN 1993 category 90) is the usual choice." },
            { "joints", "The bonded joints (welds) found by Analyse connections. Click one to show it in the model.\n\n"
                        + "Method: this joint's own method, or the study's.\n"
                        + "Category: the standard's detail category, needed with the nominal method." },
            { "col:conn", "The joint, as on the Connections tab. Click to show it in the model." },
            { "col:weldk", "The last assessment's fatigue factor of the weld (the member side of the joint): how much every "
                           + "stress could rise before the damage reaches 1.\nBelow 1 (red) fails. The list is in increasing order, "
                           + "so the joints that fail come first." },
            { "col:wallk", "The last assessment's fatigue factor of the wall the joint sits on (hand formulae for SHS "
                           + "T-joints; indicative when read from the bonded model).\nBelow 1 (red) fails. Blank: not assessed." },
            { "col:method", "This joint's method: the study's (above), structural stress (Dong) or nominal stress." },
            { "col:cat", "The standard's weld detail category for this joint (e.g. BS 7608 class F, EN 1993 category 71).\n"
                         + "Needed with the nominal method." },
            { "thread", "How the bolt threads were made, for EN 13001-3-1: rolled threads last longer than cut (machined) ones.\n"
                        + "Bolts are assessed only when they are preloaded." },
            { "key", "The result of the last fatigue assessment.\n\n"
                     + "Damage: the share of the fatigue life used over the design life (1 = a crack is expected).\n"
                     + "Fatigue factor: how much every stress could rise before the damage reaches 1 (more than 1 passes)." },
        };

        /// <summary>The column header tool tips of a grid, from FatTips' "col:" entries.</summary>
        private static void ColumnTips(DataGridView g)
        {
            foreach (DataGridViewColumn c in g.Columns)
            {
                string tip;
                if (FatTips.TryGetValue("col:" + c.Name, out tip)) c.ToolTipText = WrapTip.Wrap(tip);
            }
        }

        private List<string> fatCases = new List<string>();     // the study's load cases, for a new spectrum row
        private readonly Button fatAssess = new Button { Text = "Assess fatigue", AutoSize = true },
                                fatReport = new Button { Text = "Fatigue report", AutoSize = true },
                                fatSummary = new Button { Text = "Summary", AutoSize = true },
                                fatData = new Button { Text = "Data", AutoSize = true },
                                fatCancel = new Button { Text = "Cancel", AutoSize = true };
        private DateTime? fatChangedAt;                         // fatigue settings changed since the last assessment
        private string fatStateShown;                           // what the idle activity line last said

        /// <summary>The Fatigue tab's bar and buttons from the study folder (the user, 2026-10-17): Assess only with
        /// current static results of a full run; the documents only with fatigue results; the bar green only when the
        /// fatigue results are for the static results and settings as they are now, else red. Not while a job runs.</summary>
        private void ShowFatigueState(bool running)
        {
            if (Doc == null || StudyName == null)
            {
                foreach (var b in new[] { fatAssess, fatReport, fatSummary, fatData, fatCancel }) b.Enabled = false;
                return;
            }
            string res = Path.Combine(host.StudyFolder(Doc, StudyName), "results");
            string stat = Path.Combine(res, "results.json"), fat = Path.Combine(res, "fatigue.json");
            bool haveStatic = File.Exists(stat), haveFat = File.Exists(fat);
            bool staticOk = haveStatic && !runStale;
            bool fatOk = haveFat && staticOk && File.GetLastWriteTimeUtc(fat) >= File.GetLastWriteTimeUtc(stat)
                         && (fatChangedAt == null || fatChangedAt.Value.ToUniversalTime() < File.GetLastWriteTimeUtc(fat));
            fatCancel.Enabled = running;
            fatAssess.Enabled = !running && staticOk;
            fatReport.Enabled = fatSummary.Enabled = fatData.Enabled = !running && haveFat;
            if (running) { fatStateShown = null; return; }       // the job's activity on the line meanwhile
            fatProgress.Style = ProgressBarStyle.Continuous;
            fatProgress.Value = 100;
            fatProgress.NeedsRun = !fatOk;
            // disabled buttons show no tool tip: the reason goes on the activity line
            string why = !haveStatic ? "Run the static study first (Run tab, a full run): fatigue is worked out from its results."
                       : !staticOk ? "The static results are out of date (the study or model changed): run the static study again first."
                       : !haveFat ? "Not assessed yet: set the spectrum, then Assess fatigue."
                       : !fatOk ? "The fatigue results are out of date: Assess fatigue again."
                       : "";
            if (why != fatStateShown) { fatActivity.Text = why; fatStateShown = why; }
            tips.SetToolTip(fatProgress, fatOk ? "The fatigue results are those of the static results and fatigue settings as they are now."
                                               : why);
        }

        private static readonly string[] WeldNames = { "Structural stress (Dong): no category needed", "Nominal stress: a category per joint" };
        private static readonly string[] CurveKinds = { "Built in (SolidWorks' or AsmFEA's)", "Basquin fit of your data", "Class of the standard" };

        private TabPage FatigueTab()
        {
            var p = new TabPage("Fatigue") { AutoScroll = true };
            fatigueTab = p;
            fatStandard.Items.AddRange(FatigueSettings.Standards.Select(s => (object)s[1]).ToArray());
            fatMean.Items.AddRange(new object[] { "Goodman", "Gerber", "None" });
            fatWeld.Items.AddRange(WeldNames);
            fatThread.Items.AddRange(new object[] { "rolled", "machined" });

            fatSpectrum.Columns.Add(new DataGridViewComboBoxColumn { Name = "case", HeaderText = "Max case", FlatStyle = FlatStyle.Flat });
            fatSpectrum.Columns.Add("max", "Max %");
            fatSpectrum.Columns.Add(new DataGridViewComboBoxColumn { Name = "mincase", HeaderText = "Min case", FlatStyle = FlatStyle.Flat });
            fatSpectrum.Columns.Add("min", "Min %");
            fatSpectrum.Columns.Add("cycles", "Cycles");
            fatMaterials.AllowUserToAddRows = fatMaterials.AllowUserToDeleteRows = false;
            fatMaterials.RowHeadersVisible = fatJoints.RowHeadersVisible = false;   // nothing to add or delete: the width for data
            fatMaterials.Columns.Add(new DataGridViewTextBoxColumn { Name = "material", HeaderText = "Material", ReadOnly = true });
            fatMaterials.Columns.Add(new DataGridViewComboBoxColumn { Name = "kind", HeaderText = "S-N data", FlatStyle = FlatStyle.Flat, DataSource = CurveKinds });
            fatMaterials.Columns.Add("sf", "Sf' (MPa)");
            fatMaterials.Columns.Add("b", "b");
            fatMaterials.Columns.Add(new DataGridViewComboBoxColumn { Name = "cls", HeaderText = "Class", FlatStyle = FlatStyle.Flat });
            fatMaterials.Columns.Add(new DataGridViewComboBoxColumn { Name = "surface", HeaderText = "Surface (built in)", FlatStyle = FlatStyle.Flat,
                                                                      DataSource = FatigueSettings.Surfaces });
            fatMaterials.Columns.Add("source", "Source of the data");
            fatJoints.AllowUserToAddRows = fatJoints.AllowUserToDeleteRows = false;
            fatJoints.Columns.Add(new DataGridViewTextBoxColumn { Name = "conn", HeaderText = "Bonded joint", ReadOnly = true });
            fatJoints.Columns.Add(new DataGridViewTextBoxColumn { Name = "weldk", HeaderText = "Weld factor", ReadOnly = true });
            fatJoints.Columns.Add(new DataGridViewTextBoxColumn { Name = "wallk", HeaderText = "Wall factor", ReadOnly = true });
            fatJoints.Columns.Add(new DataGridViewComboBoxColumn { Name = "method", HeaderText = "Method", FlatStyle = FlatStyle.Flat,
                                                                   DataSource = new[] { "(study's)", "Structural (Dong)", "Nominal" } });
            fatJoints.Columns.Add("cat", "Category");

            var assess = fatAssess;
            var report = fatReport;
            var summary = fatSummary;
            var data = fatData;
            var cancel = fatCancel;
            cancel.Click += (s, e) => CancelJob();
            assess.Click += (s, e) => { if (StoreFatigue(true)) Run("fatigue", fatigueOnly: true); };
            report.Click += (s, e) => OpenFatigueDoc("report");
            summary.Click += (s, e) => OpenFatigueDoc("summary");
            data.Click += (s, e) => OpenFatigueDoc("data");
            foreach (var pair in new[] { Tuple.Create((Control)assess, "assess"), Tuple.Create((Control)cancel, "cancel"),
                                         Tuple.Create((Control)report, "report"), Tuple.Create((Control)summary, "summary"),
                                         Tuple.Create((Control)data, "data"),
                                         Tuple.Create((Control)fatStandard, "standard"), Tuple.Create((Control)fatBlocks, "blocks"),
                                         Tuple.Create((Control)fatSpectrum, "spectrum"), Tuple.Create((Control)fatMaterials, "materials"),
                                         Tuple.Create((Control)fatMean, "mean"), Tuple.Create((Control)fatGammaF, "gammaF"),
                                         Tuple.Create((Control)fatGammaM, "gammaM"), Tuple.Create((Control)fatWeld, "weld"),
                                         Tuple.Create((Control)fatStruct, "struct"), Tuple.Create((Control)fatJoints, "joints"),
                                         Tuple.Create((Control)fatThread, "thread"), Tuple.Create((Control)fatKey, "key") })
                tips.SetToolTip(pair.Item1, FatTips[pair.Item2]);
            foreach (var g in new[] { fatSpectrum, fatMaterials, fatJoints }) ColumnTips(g);
            // the columns' shares of the width: names and sources wide, numbers narrow (D119)
            foreach (var w in new[] { Tuple.Create(fatSpectrum, "case", 130), Tuple.Create(fatSpectrum, "mincase", 130),
                                      Tuple.Create(fatSpectrum, "max", 70), Tuple.Create(fatSpectrum, "min", 70),
                                      Tuple.Create(fatSpectrum, "cycles", 90), Tuple.Create(fatMaterials, "material", 150),
                                      Tuple.Create(fatMaterials, "kind", 110), Tuple.Create(fatMaterials, "sf", 70),
                                      Tuple.Create(fatMaterials, "b", 60), Tuple.Create(fatMaterials, "cls", 60),
                                      Tuple.Create(fatMaterials, "surface", 80), Tuple.Create(fatMaterials, "source", 150),
                                      Tuple.Create(fatJoints, "conn", 120), Tuple.Create(fatJoints, "weldk", 70),
                                      Tuple.Create(fatJoints, "wallk", 90), Tuple.Create(fatJoints, "method", 100),
                                      Tuple.Create(fatJoints, "cat", 70) })
                w.Item1.Columns[w.Item2].FillWeight = w.Item3;
            // each grid as tall as its rows (up to a limit, then it scrolls): no empty grey block under a short list
            foreach (var g in new[] { fatSpectrum, fatMaterials, fatJoints })
            {
                var grid = g;
                int most = grid == fatJoints ? 10 : 6;
                Action fit = () => grid.Height = grid.ColumnHeadersHeight + Math.Max(2, Math.Min(most, grid.Rows.Count)) * grid.RowTemplate.Height + 4;
                grid.RowsAdded += (s, e) => fit();
                grid.RowsRemoved += (s, e) => fit();
                fit();
            }
            // a new spectrum row starts on the first load case at both ends, 100 % to 0 % (the user's request, 2026-10-17)
            fatSpectrum.DefaultValuesNeeded += (s, e) =>
            {
                if (fatCases.Count == 0) return;
                e.Row.Cells["case"].Value = fatCases[0];
                e.Row.Cells["mincase"].Value = fatCases[0];
                e.Row.Cells["max"].Value = "100";
                e.Row.Cells["min"].Value = "0";
            };
            // a joint clicked: shown in the model as on the Connections tab (red balls round its faces)
            fatJoints.SelectionChanged += (s, e) => { if (!showing && fatJoints.ContainsFocus) ShowFatigueJoints(); };   // the user's click, not the grid's first selection

            // one column as wide as the tab (D119: the rows and grids ran off its right edge); a section per subject
            var t = new TableLayoutPanel { ColumnCount = 1, AutoSize = true, Dock = DockStyle.Top, Padding = new Padding(4, 2, 4, 4) };
            t.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            Action<Control> add = c => { c.Dock = DockStyle.Top; t.Controls.Add(c); };
            Action<string> heading = text => add(Caption(text, true));
            // as the Run tab: the buttons, the bar full width below them, the activity; then the result and the reports
            Primary(assess);
            add(Row(assess, cancel));
            add(fatProgress);
            add(fatActivity);
            add(fatKey);
            add(Row(Caption("Reports:"), report, summary, data));
            add(Rule());
            heading("Standard and design life");
            add(Row(Caption("Standard"), fatStandard));
            add(Row(Caption("Design life: the spectrum"), fatBlocks, Caption("times")));
            heading("Loading spectrum");
            add(Note("Each row: a load case cycling between two percentages of it."));
            add(fatSpectrum);
            heading("S-N data per material");
            add(Note("Built in, your own data, or a class of the standard (unwelded material)."));
            add(fatMaterials);
            add(Row(Caption("Mean stress (your curves)"), fatMean));
            add(Row(Caption("Partial factors: on stress"), fatGammaF, Caption("on strength"), fatGammaM));
            heading("Bonded joints (welds)");
            add(Row(fatWeld));
            add(Row(Caption("Weld curve (structural stress)"), fatStruct));
            add(fatJoints);
            heading("Bolts");
            add(Row(Caption("Threads (EN 13001-3-1)"), fatThread));
            add(fatProblems);
            p.Controls.Add(t);
            fatKey.Resize += (s, e) => FitHeight(fatKey, 20);
            fatKey.TextChanged += (s, e) => FitHeight(fatKey, 20);
            fatProblems.TextChanged += (s, e) => { if (fatProblems.Text.Length == 0) fatProblems.Height = 0; else FitHeight(fatProblems); };

            foreach (var cb in new[] { fatStandard, fatMean, fatWeld, fatStruct, fatThread })
                cb.SelectedIndexChanged += (s, e) =>
                {
                    if (showing) return;
                    if (cb == fatStandard) { FillStandardChoices(); }
                    StoreFatigue(false);
                };
            foreach (var tb in new[] { fatBlocks, fatGammaF, fatGammaM }) tb.TextChanged += (s, e) => { if (!showing) StoreFatigue(false); };
            foreach (var g in new[] { fatSpectrum, fatMaterials, fatJoints })
            {
                g.CellEndEdit += (s, e) => { if (!showing) StoreFatigue(false); };
                if (g == fatMaterials)                            // the S-N choice changed: lock or free its cells
                    g.CellEndEdit += (s, e) => { if (!showing && e.ColumnIndex == fatMaterials.Columns["kind"].Index) { ShowSnData(); ShowClassColumn(); } };
                g.UserDeletedRow += (s, e) => { if (!showing) StoreFatigue(false); };
                g.DataError += (s, e) => { e.ThrowException = false; };          // a stored value no longer in a list
            }
            return p;
        }

        private string FatStandardKey()
        {
            int i = fatStandard.SelectedIndex;
            return i >= 0 ? FatigueSettings.Standards[i][0] : "bs7608";
        }

        private void FillStandardChoices()
        {
            string std = FatStandardKey();
            string was = fatStruct.SelectedItem as string;
            fatStruct.Items.Clear();
            fatStruct.Items.AddRange(FatigueSettings.StructuralChoices(std).Cast<object>().ToArray());
            fatStruct.SelectedItem = was != null && fatStruct.Items.Contains(was) ? was : FatigueSettings.StructuralDefault(std);
            var cls = (DataGridViewComboBoxColumn)fatMaterials.Columns["cls"];
            cls.DataSource = FatigueSettings.ParentClasses(std);
            fatThread.Enabled = std == "en13001_3_1";
        }

        /// <summary>The folder's model.json materials and connections.json bonded joints (after an export and an
        /// Analyse connections); empty lists before.</summary>
        private void FolderLists(out List<string> materials, out List<string> joints)
        {
            materials = new List<string>();
            joints = new List<string>();
            if (Doc == null || StudyName == null) return;
            string folder = host.StudyFolder(Doc, StudyName);
            try
            {
                var m = Json.Parse(File.ReadAllText(Path.Combine(folder, "model.json"))) as JObj;
                var used = new HashSet<string>(((m?["parts"] as List<object>) ?? new List<object>()).OfType<JObj>().Select(x => (string)x["material"]));
                materials = ((m?["materials"] as List<object>) ?? new List<object>()).OfType<JObj>()
                            .Select(x => (string)x["name"]).Where(used.Contains).ToList();
            }
            catch (Exception) { }
            try
            {
                var c = Json.Parse(File.ReadAllText(Path.Combine(folder, "connections.json"))) as JObj;
                joints = ((c?["connections"] as List<object>) ?? new List<object>()).OfType<JObj>()
                         .Where(x => (string)x["type"] == "tie").Select(x => (string)x["id"]).ToList();
            }
            catch (Exception) { }
        }

        private void ClearFatigue()
        {
            fatSpectrum.Rows.Clear();
            fatMaterials.Rows.Clear();
            fatJoints.Rows.Clear();
            fatKey.Text = fatProblems.Text = "";
        }

        /// <summary>Show the study's fatigue settings (called with showing set).</summary>
        private void ShowFatigue(JObj st)
        {
            ClearFatigue();
            if (st == null) return;
            var f = FatigueSettings.Get(Settings(st), false);
            string std = (string)f?["standard"] ?? "bs7608";
            fatStandard.SelectedIndex = Math.Max(0, Array.FindIndex(FatigueSettings.Standards, s => s[0] == std));
            FillStandardChoices();
            var cases = Cases(st);
            fatCases = cases.ToList();
            var caseCol = (DataGridViewComboBoxColumn)fatSpectrum.Columns["case"];
            var minCol = (DataGridViewComboBoxColumn)fatSpectrum.Columns["mincase"];
            caseCol.DataSource = cases.ToList();
            minCol.DataSource = new[] { "" }.Concat(cases).ToList();
            foreach (var r in ((f?["spectrum"] as List<object>) ?? new List<object>()).OfType<JObj>())
                fatSpectrum.Rows.Add((string)r["case"], Fmt(r["max_pct"]), (string)r["min_case"] ?? "", Fmt(r["min_pct"]), Fmt(r["cycles"]));
            fatBlocks.Text = f?["design_blocks"] != null ? Fmt(f["design_blocks"]) : "1";
            var parent = f?["parent"] as JObj;
            string ms = (string)parent?["mean_stress"] ?? "goodman";
            fatMean.SelectedIndex = Array.IndexOf(FatigueSettings.MeanStress, ms);
            fatGammaF.Text = f?["gamma_Ff"] != null ? Fmt(f["gamma_Ff"]) : "1";
            fatGammaM.Text = f?["gamma_Mf"] != null ? Fmt(f["gamma_Mf"]) : "1";
            var welds = f?["welds"] as JObj;
            fatWeld.SelectedIndex = (string)welds?["method"] == "nominal" ? 1 : 0;
            if (welds?["structural_category"] != null) fatStruct.SelectedItem = (string)welds["structural_category"];
            fatThread.SelectedItem = (string)(f?["bolts"] as JObj)?["thread"] ?? "rolled";
            List<string> mats, joints;
            FolderLists(out mats, out joints);
            var stored = ((f?["materials"] as List<object>) ?? new List<object>()).OfType<JObj>().ToList();
            foreach (var m in mats.Union(stored.Select(x => (string)x["material"])))
            {
                var e = stored.FirstOrDefault(x => (string)x["material"] == m);
                var bq = e?["basquin"] as JObj;
                string kind = e?["standard_class"] != null ? CurveKinds[2] : bq != null ? CurveKinds[1] : CurveKinds[0];
                // in the columns' order: ..., class, surface, source
                fatMaterials.Rows.Add(m, kind, Fmt(bq?["Sf"]), Fmt(bq?["b"]), (string)e?["standard_class"], (string)e?["surface"] ?? "",
                                      (string)e?["source"] ?? "");
            }
            var per = ((welds?["joints"] as List<object>) ?? new List<object>()).OfType<JObj>().ToList();
            // lowest fatigue factor first, so the joints that fail are at the top (the user, 2026-10-17)
            var results = FatigueResults();
            fatFilledFrom = FatigueResultsTime();
            foreach (var jf in FatigueSettings.JointOrder(joints.Union(per.Select(x => (string)x["connection"])), results))
            {
                var e = per.FirstOrDefault(x => (string)x["connection"] == jf.Id);
                string meth = (string)e?["method"] == "nominal" ? "Nominal" : (string)e?["method"] == "dong" ? "Structural (Dong)" : "(study's)";
                int i = fatJoints.Rows.Add(jf.Id, FatigueSettings.FactorText(jf.Weld),
                                           FatigueSettings.FactorText(jf.Wall) + (jf.WallIndicative ? " (indicative)" : ""),
                                           meth, (string)e?["category"] ?? "");
                foreach (var c in new[] { Tuple.Create("weldk", jf.Weld), Tuple.Create("wallk", jf.Wall) })
                    if (c.Item2 < 1) fatJoints.Rows[i].Cells[c.Item1].Style.ForeColor = System.Drawing.Color.Firebrick;   // fails
            }
            ShowFatigueProblems(st);
            ShowClassColumn();
            ShowSnData();
            RefreshFatigue();
        }

        /// <summary>The S-N grid's Class column only while a material uses a class of the standard (D119: seven columns
        /// did not fit the pane).</summary>
        private void ShowClassColumn()
        {
            fatMaterials.Columns["cls"].Visible = fatMaterials.Rows.Cast<DataGridViewRow>()
                                                  .Any(r => (string)r.Cells["kind"].Value == CurveKinds[2]);
        }

        /// <summary>The curve fatigue will use for each material set to "Built in" (the SolidWorks material's or
        /// AsmFEA's, D113): shown in its row, greyed and read-only, with its source; measured or ESTIMATED.</summary>
        private void ShowSnData()
        {
            if (Doc == null || StudyName == null) return;
            string folder = host.StudyFolder(Doc, StudyName);
            if (!File.Exists(Path.Combine(folder, "model.json"))) return;
            Cli.RunAsync(this, "sn-data " + Cli.Quote(folder), (r, err) =>
            {
                var mats = (r as JObj)?["materials"] as JObj;
                if (err != null || mats == null) return;
                bool was = showing;
                showing = true;
                try
                {
                    foreach (DataGridViewRow row in fatMaterials.Rows)
                    {
                        var d = mats[(string)row.Cells["material"].Value] as JObj;
                        bool builtin = (string)row.Cells["kind"].Value == CurveKinds[0];
                        foreach (var name in new[] { "sf", "b", "source" })
                        {
                            row.Cells[name].ReadOnly = builtin;
                            row.Cells[name].Style.ForeColor = builtin ? System.Drawing.Color.DimGray : System.Drawing.Color.Empty;
                        }
                        if (!builtin || d == null) continue;
                        if ((string)d["from"] == "none")
                        {
                            row.Cells["source"].Value = "none: give S-N data (no tensile strength to estimate from)";
                            continue;
                        }
                        row.Cells["sf"].Value = d["Sf"] != null ? Fmt(d["Sf"]) : "";
                        row.Cells["b"].Value = d["b"] != null ? Fmt(d["b"]) : "";
                        string at = " (" + Fmt(d["amplitude_1e6"]) + " MPa at 1e6 cycles)";
                        row.Cells["source"].Value = ((d["estimated"] is bool e && e) ? "ESTIMATED: " : "") + (string)d["name"] + at;
                        row.Cells["source"].ToolTipText = WrapTip.Wrap((string)d["source"]);
                    }
                }
                finally { showing = was; }
            });
        }

        /// <summary>Store the tab into the study (no Apply). A fatigue change needs no new solve, so the Run bar's
        /// "solve again" state is left alone. quiet = false: Say what was stored. Returns false when Assess cannot
        /// start (fatigue off, or problems).</summary>
        private bool StoreFatigue(bool forAssess)
        {
            if (Study == null) return false;
            var s = Settings(Study);
            var f = FatigueSettings.Get(s, true);
            f["standard"] = FatStandardKey();
            var rows = new List<object>();
            var bad = new List<string>();
            foreach (DataGridViewRow r in fatSpectrum.Rows)
            {
                if (r.IsNewRow) continue;
                string why;
                var row = FatigueSettings.Row((string)r.Cells["case"].Value, Convert.ToString(r.Cells["max"].Value),
                                              (string)r.Cells["mincase"].Value, Convert.ToString(r.Cells["min"].Value),
                                              Convert.ToString(r.Cells["cycles"].Value), out why);
                if (row != null) rows.Add(row); else bad.Add("Row " + (r.Index + 1) + ": " + why + ".");
            }
            f["spectrum"] = rows;
            var blocks = Num(fatBlocks.Text);
            if (blocks > 0) f["design_blocks"] = blocks.Value; else f.Remove("design_blocks");
            f["parent"] = new JObj { { "mean_stress", FatigueSettings.MeanStress[Math.Max(0, fatMean.SelectedIndex)] } };
            foreach (var pair in new[] { Tuple.Create(fatGammaF, "gamma_Ff"), Tuple.Create(fatGammaM, "gamma_Mf") })
            {
                var v = Num(pair.Item1.Text);
                if (v >= 1 && v != 1) f[pair.Item2] = v.Value; else f.Remove(pair.Item2);
            }
            f.Remove("materials");
            foreach (DataGridViewRow r in fatMaterials.Rows)
            {
                string k = (string)r.Cells["kind"].Value;
                string kind = k == CurveKinds[2] ? "class" : k == CurveKinds[1] ? "basquin" : "builtin";
                FatigueSettings.SetMaterial(f, (string)r.Cells["material"].Value, kind, Num(Convert.ToString(r.Cells["sf"].Value)),
                                            Num(Convert.ToString(r.Cells["b"].Value)), (string)r.Cells["cls"].Value,
                                            Convert.ToString(r.Cells["source"].Value), (string)r.Cells["surface"].Value);
            }
            var welds = new JObj { { "method", fatWeld.SelectedIndex == 1 ? "nominal" : "dong" } };
            string sc = fatStruct.SelectedItem as string;
            if (sc != null && sc != FatigueSettings.StructuralDefault(FatStandardKey())) welds["structural_category"] = sc;
            var per = new List<object>();
            foreach (DataGridViewRow r in fatJoints.Rows)
            {
                string meth = (string)r.Cells["method"].Value, cat = Convert.ToString(r.Cells["cat"].Value);
                if ((meth == null || meth == "(study's)") && string.IsNullOrWhiteSpace(cat)) continue;
                var j = new JObj { { "connection", (string)r.Cells["conn"].Value } };
                if (meth == "Nominal") j["method"] = "nominal"; else if (meth == "Structural (Dong)") j["method"] = "dong";
                if (!string.IsNullOrWhiteSpace(cat)) j["category"] = cat.Trim();
                per.Add(j);
            }
            if (per.Count > 0) welds["joints"] = per;
            f["welds"] = welds;
            if (fatThread.Enabled) f["bolts"] = new JObj { { "thread", (string)fatThread.SelectedItem ?? "rolled" } }; else f.Remove("bolts");
            host.MarkChanged(Doc);                                // stored; no new solve needed (D105)
            var problems = bad.Concat(ShowFatigueProblems(Study)).ToList();
            if (forAssess && problems.Count > 0)
            {
                Say("Fatigue not assessed: " + problems[0], true);
                return false;
            }
            if (!forAssess) fatChangedAt = DateTime.Now;          // the bar red until assessed again
            if (!forAssess) Say("Fatigue settings stored in the study (no new solve needed: Assess fatigue). Save the assembly to keep them.");
            return true;
        }

        private List<string> ShowFatigueProblems(JObj st)
        {
            var f = FatigueSettings.Get(Settings(st), false);
            List<string> mats, joints;
            FolderLists(out mats, out joints);
            var list = f == null ? new List<string>() : FatigueSettings.Problems(f, Cases(st), mats);
            fatProblems.Text = string.Join("\n", list.Take(4));
            return list;
        }

        /// <summary>The key figures from the study folder's results/fatigue.json; and after a job, the material and
        /// joint lists again from the folder (an export or a run writes model.json and connections.json after the study
        /// was first shown, which left them empty: found on a user's model, 2026-10-17). The entries are stored in
        /// the study as they are made, so showing it again loses nothing.</summary>
        private DateTime? fatFilledFrom;     // the fatigue.json the joints list was ordered from

        private string FatigueResultsPath()
        {
            return Path.Combine(host.StudyFolder(Doc, StudyName), "results", "fatigue.json");
        }

        private DateTime? FatigueResultsTime()
        {
            try { return Doc == null || StudyName == null || !File.Exists(FatigueResultsPath()) ? (DateTime?)null : File.GetLastWriteTimeUtc(FatigueResultsPath()); }
            catch (Exception) { return null; }
        }

        private JObj FatigueResults()
        {
            try { return FatigueResultsTime() == null ? null : Json.Parse(File.ReadAllText(FatigueResultsPath())) as JObj; }
            catch (Exception) { return null; }
        }

        private void RefreshFatigue()
        {
            if (Doc == null || StudyName == null) { fatKey.Text = ""; return; }
            if (Study != null && !fatSpectrum.IsCurrentCellInEditMode && !fatMaterials.IsCurrentCellInEditMode
                && !fatJoints.IsCurrentCellInEditMode)
            {
                List<string> mats, joints;
                FolderLists(out mats, out joints);
                // when the folder has more than the grids (after a refill they hold at least the folder's), or a new
                // assessment has new factors to order the joints by
                if (mats.Count > fatMaterials.Rows.Count || joints.Count > fatJoints.Rows.Count || FatigueResultsTime() != fatFilledFrom)
                {
                    bool was = showing;
                    showing = true;
                    try { ShowFatigue(Study); }
                    finally { showing = was; }
                    return;                                       // ShowFatigue ends with RefreshFatigue
                }
            }
            string path = Path.Combine(host.StudyFolder(Doc, StudyName), "results", "fatigue.json");
            JObj res = null;
            try { if (File.Exists(path)) res = Json.Parse(File.ReadAllText(path)) as JObj; }
            catch (Exception) { }
            fatKey.Text = FatigueSettings.KeyFigures(res);
            // solved again since the assessment: the fatigue figures are for the old static results
            string stat = Path.Combine(Path.GetDirectoryName(path), "results.json");
            if (res != null && File.Exists(stat) && File.GetLastWriteTimeUtc(stat) > File.GetLastWriteTimeUtc(path))
                fatKey.Text = "OUT OF DATE (solved again since): Assess fatigue again.  " + fatKey.Text;
        }

        /// <summary>The bonded joints selected in the joints grid shown in the model, as the Connections tab shows
        /// connections (the colour view, D98: balls round the faces); nothing selected clears it.</summary>
        private void ShowFatigueJoints()
        {
            if (Doc == null || StudyName == null || tabs.SelectedTab != fatigueTab) return;
            var ids = new HashSet<string>(fatJoints.SelectedCells.Cast<DataGridViewCell>()
                                          .Select(c => fatJoints.Rows[c.RowIndex].Cells["conn"].Value as string).Where(x => x != null));
            if (ids.Count == 0) { host.ClearColours(); return; }
            try
            {
                string path = Path.Combine(host.StudyFolder(Doc, StudyName), "connections.json");
                var doc = Json.Parse(File.ReadAllText(path)) as JObj;
                var sides = ((doc?["connections"] as List<object>) ?? new List<object>()).OfType<JObj>()
                            .Where(c => ids.Contains((string)c["id"]))
                            .SelectMany(c => ((c["sides"] as List<object>) ?? new List<object>()).OfType<JObj>())
                            .Where(sd => sd["fingerprint"] is JObj).ToList();
                if (sides.Count == 0) { Say("That joint has no faces to show: click Analyse connections again.", true); return; }
                string why = host.ShowColours(Doc, sides, new List<string>(), true);
                if (why != null) Say("Joint view: " + why + ".", true);
            }
            catch (Exception ex) { Say("Could not show the joint: " + ex.Message, true); }
        }

        /// <summary>Open the fatigue "report", "summary" or "data" report (D114), through the CLI, which writes it
        /// again when it is missing, older than the fatigue results, or from older report code.</summary>
        private void OpenFatigueDoc(string which)
        {
            if (Doc == null || StudyName == null) return;
            string flag = which == "summary" ? " --summary" : which == "data" ? " --data" : "";
            Cli.RunAsync(this, "report " + Cli.Quote(host.StudyFolder(Doc, StudyName)) + " --fatigue" + flag, (r, err) =>
            {
                if (err != null) { Say("The fatigue " + which + " could not be opened: " + err, true); return; }
                var path = r as string ?? (string)((r as JObj)?["path"]);
                if (path != null) OpenPath(path); else Say("No fatigue " + which + " path was returned.", true);
            });
        }
    }
}
