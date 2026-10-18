// The AsmFEA SolidWorks add-in: the task pane (TaskPane.cs) and the COM automation object that
// the asmfea MCP and API reach through ISldWorks.GetAddInObject (PLAN.md "Automation object").
// It holds no analysis logic. Studies are stored in the assembly (Persistence.cs, milestone 8).
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using AsmFea.Extract;
using Microsoft.Win32;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidWorks.Interop.swpublished;

[assembly: AssemblyTitle("AsmFEA SolidWorks add-in")]
[assembly: AssemblyVersion("0.10.1.0")]
[assembly: AssemblyFileVersion("0.10.1.0")]

namespace AsmFea.AddIn
{
    /// <summary>
    /// The automation interface. It is the class's default (first) interface, so late-bound
    /// clients such as pywin32 see these methods through IDispatch.
    /// </summary>
    [ComVisible(true)]
    [Guid("37cd13c2-ea1a-4257-8990-d818128a46a4")]
    [InterfaceType(ComInterfaceType.InterfaceIsDual)]
    public interface IAsmFeaAutomation
    {
        string Version { get; }
        /// <summary>Write model.json and parts/*.step for the active assembly into `folder`; returns model.json's path.</summary>
        string ExportModel(string study, string folder);
        /// <summary>study.json of a study beside the active assembly, or "" when there is none.</summary>
        string GetStudyJson(string study);
        void SetStudyJson(string study, string json);
        /// <summary>JSON array of the study names beside the active assembly.</summary>
        string ListStudies();
        /// <summary>Select the faces of the given face_ref ids (JSON array) from the study's model.json; returns how many were found.</summary>
        int HighlightFaces(string study, string faceRefIdsJson);
        /// <summary>Show the task pane on a tab ("Loads", "Connections", "Run", "Results", "Fatigue"; the old "Study" and "Loads and fixtures" still work)
        /// and, with a path, save the pane as a PNG (documentation screenshots). Returns the pane size "w x h".</summary>
        string CapturePane(string tab, string path);
        /// <summary>Add a fixture or load (JSON) to the faces selected in SolidWorks; returns its id.</summary>
        string AddStudyItem(string study, string json);
        /// <summary>Diagnosis: select a row of the Loads list (as a click would); returns the pane's status line.</summary>
        string DebugArrow(string loadId);

        // No menu commands: everything is in the task pane (the Tools > AsmFEA > Export menu was
        // removed, D22). A command callback added later must be declared here: SolidWorks calls
        // it by name through this interface (ClassInterfaceType.None), or it silently does
        // nothing (found live, D19).
    }

    [ComVisible(true)]
    [Guid("77cf8ee6-9213-44db-8379-945fd7578fc2")]
    [ProgId("AsmFea.AddIn")]
    [ClassInterface(ClassInterfaceType.None)]
    [ComDefaultInterface(typeof(IAsmFeaAutomation))]
    public class AsmFeaAddIn : IAsmFeaAutomation, ISwAddin, IPaneHost
    {
        public const string TITLE = "AsmFEA";
        public const string PANE_TITLE = "AsmFEA: Assembly Level FEA";
        public const int MIN_YEAR = 2018;   // compiled against the SolidWorks 2018 API (build.ps1 -SwApi 26, D26)
        public const string DEFAULT_STUDY = "Study1";
        private const int CMD_GROUP_ID = 0xA5F3;

        private ISldWorks sw;
        private int cookie;
        private ICommandManager cmdMgr;
        private readonly StudyRegistry registry = new StudyRegistry();
        private TaskpaneView paneView;
        private StudyPane pane;
        private ColourView colours;           // the Connections tab's colour view (D98)
        private Tuple<IModelDoc2, IList<JObj>, ICollection<string>> colourArgs;   // what it shows, to show again after a save
        private bool selecting;               // the add-in is selecting: not the user's pick

        /// <summary>The user selected something in a document (the Connections tab's two-way selection, D98).</summary>
        public event Action<IModelDoc2> UserSelected;

        public string Version
        {
            get { return Assembly.GetExecutingAssembly().GetName().Version.ToString(3); }
        }

        // ------------------------------------------------------------------ ISwAddin

        /// <summary>The SolidWorks versions the user chose in the installer (HKLM\SOFTWARE\AsmFEA, SolidWorksVersions,
        /// e.g. "2026,2025"); null when not set, which enables every version (a development machine).</summary>
        public static HashSet<int> EnabledVersions()
        {
            try
            {
                using (var k = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\AsmFEA"))
                {
                    var v = k == null ? null : k.GetValue("SolidWorksVersions") as string;
                    if (string.IsNullOrWhiteSpace(v)) return null;
                    return new HashSet<int>(v.Split(',', ';', ' ').Select(x => x.Trim()).Where(x => x.Length > 0).Select(int.Parse));
                }
            }
            catch (Exception) { return null; }
        }

        /// <summary>SolidWorks' year from its revision number ("34.2.1" is SolidWorks 2026).</summary>
        public static int SolidWorksYear(string revision)
        {
            int major;
            return int.TryParse((revision ?? "").Split('.')[0], out major) ? major + 1992 : 0;
        }

        public bool ConnectToSW(object thisSW, int cookie)
        {
            var app = (ISldWorks)thisSW;
            // one registration serves every installed SolidWorks: stay dormant in versions the user did not choose
            var enabled = EnabledVersions();
            int year = SolidWorksYear(app.RevisionNumber());
            if (enabled != null && !enabled.Contains(year)) return true;
            if (year > 0 && year < MIN_YEAR)
            {
                app.SendMsgToUser2($"AsmFEA needs SOLIDWORKS {MIN_YEAR} or later; this is SOLIDWORKS {year}. It will not load.",
                                   (int)swMessageBoxIcon_e.swMbWarning, (int)swMessageBoxBtn_e.swMbOk);
                return true;
            }
            sw = app;
            this.cookie = cookie;
            sw.SetAddinCallbackInfo2(0, this, cookie);
            // the old Tools > AsmFEA menu: SolidWorks remembers command groups, so remove it once
            cmdMgr = sw.GetCommandManager(cookie);
            try { cmdMgr.RemoveCommandGroup2(CMD_GROUP_ID, true); } catch { }

            // studies live in the assemblies: watch every assembly that is or becomes open
            colours = new ColourView(sw);
            registry.UserSelected += d => { if (!selecting) UserSelected?.Invoke(d); };
            // never saved with the view's colours: off before the write (LIVE-T13), back on after it
            registry.BeforeSave += d => { if (colours != null && colours.IsOn) colours.Clear(); };
            registry.AfterSave += d =>
            {
                if (colourArgs != null && !colours.IsOn && ReferenceEquals(colourArgs.Item1, d))
                    try { colours.Show(colourArgs.Item1, colourArgs.Item2, colourArgs.Item3, false); } catch { }
            };
            registry.Leaving += d => { if (colours != null && colours.IsOn) { colours.Clear(); colourArgs = null; } };
            var events = (SldWorks)sw;
            events.DocumentLoadNotify2 += OnDocumentLoad;
            events.FileNewNotify2 += (object doc, int type, string template) => { registry.Attach(doc as IModelDoc2); return 0; };
            events.ActiveModelDocChangeNotify += OnActiveDocChange;
            events.FileCloseNotify += OnFileClose;
            foreach (var d in ((object[])sw.GetDocuments() ?? new object[0]).OfType<IModelDoc2>()) registry.Attach(d);
            try { CreatePane(); }
            catch (Exception e) { sw.SendMsgToUser2("AsmFEA: the task pane could not be created: " + e.Message, (int)swMessageBoxIcon_e.swMbWarning, (int)swMessageBoxBtn_e.swMbOk); }
            return true;
        }

        public bool DisconnectFromSW()
        {
            if (sw == null) return true;   // dormant in this SolidWorks version
            try { cmdMgr?.RemoveCommandGroup2(CMD_GROUP_ID, true); } catch { }
            try
            {
                var app = (SldWorks)sw;
                app.DocumentLoadNotify2 -= OnDocumentLoad;
                app.ActiveModelDocChangeNotify -= OnActiveDocChange;
                app.FileCloseNotify -= OnFileClose;
            }
            catch { }
            try { colours?.Clear(); } catch { }
            try { paneView?.DeleteView(); } catch { }
            pane?.Dispose();
            pane = null;
            paneView = null;
            cmdMgr = null;
            sw = null;
            GC.Collect();
            GC.WaitForPendingFinalizers();
            return true;
        }

        private int OnDocumentLoad(string title, string path)
        {
            try { pane?.DocumentOpened(path); } catch { }
            foreach (var d in ((object[])sw.GetDocuments() ?? new object[0]).OfType<IModelDoc2>())
                if (string.Equals(d.GetPathName(), path, StringComparison.OrdinalIgnoreCase))
                {
                    registry.Attach(d);
                    try { ColourView.RestoreLeftovers(d); } catch { }    // a colour view a crash left in it (D98)
                }
            return 0;
        }

        // Closing the last assembly fires no active-document change, so the pane kept showing it (found live).
        // The closing file is dropped from the pane at once (SolidWorks still reports it active at this point, so a
        // plain refresh filled the pane again from it: found live), then refreshed twice more as SolidWorks lets go.
        private int OnFileClose(string fileName, int reason)
        {
            StudyPane.Trace("FileCloseNotify: " + fileName + " (reason " + reason + ")");
            try
            {
                if (pane == null || !pane.IsHandleCreated) return 0;
                pane.BeginInvoke(new Action(() =>
                {
                    try { ShowArrow(null, null, null, false); pane.DocumentClosed(fileName); } catch { }
                    foreach (int ms in new[] { 400, 1500 })
                    {
                        var t = new System.Windows.Forms.Timer { Interval = ms };
                        t.Tick += (s, e) => { t.Stop(); t.Dispose(); try { pane.RefreshDocument(); } catch { } };
                        t.Start();
                    }
                }));
            }
            catch { }
            return 0;
        }

        private int OnActiveDocChange()
        {
            var active = sw.ActiveDoc as IModelDoc2;
            if (colours != null && colours.IsOn && !ReferenceEquals(colours.Doc, active)) { colours.Clear(); colourArgs = null; }
            registry.Attach(active);
            try { pane?.RefreshDocument(); } catch { }
            return 0;
        }

        private void CreatePane()
        {
            // the tab icon at every size SolidWorks asks for (DPI scaling), drawn at start-up;
            // older SolidWorks takes one 16 x 16 bitmap
            try { paneView = sw.CreateTaskpaneView3(Icons.Pngs(20, 32, 40, 64, 96, 128), PANE_TITLE); }
            catch (Exception) { paneView = null; }
            if (paneView == null)
            {
                string icon = Path.Combine(Path.GetTempPath(), "asmfea_pane.bmp");
                using (var bmp = Icons.Icon(16, System.Drawing.Color.White)) bmp.Save(icon, System.Drawing.Imaging.ImageFormat.Bmp);
                paneView = sw.CreateTaskpaneView2(icon, PANE_TITLE);
            }
            pane = new StudyPane(this);
            pane.CreateControl();
            paneView.DisplayWindowFromHandlex64(pane.Handle.ToInt64());
            pane.RefreshDocument();
        }

        // ------------------------------------------------------------------ automation

        public string ExportModel(string study, string folder)
        {
            try
            {
                var doc = RequireAssembly();
                IList<string> log;
                return Export(doc, study, out log, string.IsNullOrWhiteSpace(folder) ? null : folder);
            }
            catch (Exception e)
            {
                throw new COMException("AsmFEA ExportModel: " + e.Message);
            }
        }

        /// <summary>The study as stored in the assembly (study.json shape), or "" when there is none.</summary>
        public string GetStudyJson(string study)
        {
            var s = registry.StoreFor(RequireAssembly()).Get(study);
            return s == null ? "" : Json.Write(s);
        }

        /// <summary>Replace the study stored in the assembly; it reaches disk when the user saves.</summary>
        public void SetStudyJson(string study, string json)
        {
            JObj s;
            try { s = Json.Parse(json) as JObj; }
            catch (Exception e) { throw new COMException("AsmFEA SetStudyJson: not valid JSON: " + e.Message); }
            if (s == null) throw new COMException("AsmFEA SetStudyJson: the JSON is not an object");
            CheckStudyName(study);
            var doc = RequireAssembly();
            registry.StoreFor(doc).Set(study, s);
            registry.MarkChanged(doc);
            RefreshPane();
        }

        /// <summary>
        /// Add a fixture or load to the faces selected in SolidWorks, the way the task pane does (same face
        /// references). json: {"kind": "fixture"|"load", ...the item's fields: type, dofs, hold_rotations,
        /// load_case, magnitude, direction...}. Gravity needs no faces. Returns the new item's id.
        /// </summary>
        public string AddStudyItem(string study, string json)
        {
            JObj item;
            try { item = Json.Parse(json) as JObj; }
            catch (Exception e) { throw new COMException("AsmFEA AddStudyItem: not valid JSON: " + e.Message); }
            if (item == null || (string)item["kind"] != "fixture" && (string)item["kind"] != "load")
                throw new COMException("AsmFEA AddStudyItem: kind must be fixture or load");
            CheckStudyName(study);
            var doc = RequireAssembly();
            var st = registry.StoreFor(doc).GetOrCreate(study);
            bool load = (string)item["kind"] == "load";
            var faces = new List<object>();
            if (!(load && (string)item["type"] == "gravity"))
            {
                var sel = SelectedFaces(doc);
                if (sel.Count == 0) throw new COMException("AsmFEA AddStudyItem: select one or more faces first");
                foreach (var f in sel)
                {
                    var fr = FaceRefFromSelection(doc, f, StudyStore.NextId(st, "face_refs", "L"));
                    var same = StudyStore.List(st, "face_refs").OfType<JObj>()
                               .FirstOrDefault(o => (string)o["persistent_ref"] == (string)fr["persistent_ref"]);
                    if (same != null) { faces.Add(same["id"]); continue; }
                    StudyStore.List(st, "face_refs").Add(fr);
                    faces.Add(fr["id"]);
                }
            }
            string key = load ? "loads" : "fixtures";
            var o2 = new JObj { { "id", (string)item["id"] ?? StudyStore.NextId(st, key, load ? "LD" : "FX") } };
            foreach (var kv in item) if (kv.Key != "kind" && kv.Key != "id") o2[kv.Key] = kv.Value;
            if (faces.Count > 0) o2["faces"] = faces;
            StudyStore.List(st, key).Add(o2);
            registry.MarkChanged(doc);
            RefreshPane();
            return (string)o2["id"];
        }

        /// <summary>Refresh the pane on its own thread: automation calls arrive on an RPC thread.</summary>
        private void RefreshPane()
        {
            try { if (pane != null && pane.IsHandleCreated) pane.BeginInvoke(new Action(() => pane.RefreshDocument())); }
            catch { }
        }

        public string ListStudies()
        {
            return Json.Write(registry.StoreFor(RequireAssembly()).Names.Cast<object>().ToList()).Trim();
        }

        public string DebugArrow(string loadId)
        {
            if (pane == null || paneView == null) throw new COMException("AsmFEA DebugArrow: the task pane is not open");
            return (string)pane.Invoke(new Func<string>(() => pane.SelectLoadRow(loadId)));
        }

        public string CapturePane(string tab, string path)
        {
            if (pane == null || paneView == null) throw new COMException("AsmFEA CapturePane: the task pane is not open");
            paneView.ShowView();
            // automation calls from another process arrive on an RPC thread (.NET COM objects are
            // "Both"-threaded): the pane's controls may only be touched on its own UI thread
            string file = string.IsNullOrWhiteSpace(path) ? null : path;
            return (string)pane.Invoke(new Func<string>(() => pane.Screenshot(tab, file)));
        }

        public int HighlightFaces(string study, string faceRefIdsJson)
        {
            var doc = RequireAssembly();
            var ids = ((List<object>)Json.Parse(faceRefIdsJson)).Cast<string>().ToList();
            // the study's own faces first (loads and fixtures), then the exported model's (mates)
            var s = registry.StoreFor(doc).Get(study);
            int n = s == null ? 0 : SelectFaceRefs(doc, StudyStore.List(s, "face_refs"), ids, false);
            string modelPath = Path.Combine(StudyFolder(doc, study), "model.json");
            if (File.Exists(modelPath))
            {
                var model = (JObj)Json.Parse(File.ReadAllText(modelPath, Encoding.UTF8));
                n += SelectFaceRefs(doc, (List<object>)model["face_refs"] ?? new List<object>(), ids, n > 0);
            }
            return n;
        }

        private int SelectFaceRefs(IModelDoc2 doc, List<object> refs, ICollection<string> ids, bool append)
        {
            selecting = true;
            try { return SelectFaceRefsNow(doc, refs, ids, append); }
            finally { selecting = false; }
        }

        private static int SelectFaceRefsNow(IModelDoc2 doc, List<object> refs, ICollection<string> ids, bool append)
        {
            var faces = new List<object>();
            foreach (JObj fr in refs.OfType<JObj>())
            {
                if (!ids.Contains((string)fr["id"]) || fr["persistent_ref"] == null) continue;
                int err;
                var ent = doc.Extension.GetObjectByPersistReference3(Convert.FromBase64String((string)fr["persistent_ref"]), out err);
                if (ent != null) faces.Add(ent);
            }
            if (!append) doc.ClearSelection2(true);              // selection is view state; the document is not modified
            // one by one: MultiSelect2 given a .NET object[] selected nothing (found live)
            foreach (var f in faces) ((IEntity)f).Select4(true, null);
            doc.GraphicsRedraw2();
            var sm = (ISelectionMgr)doc.SelectionManager;
            return Math.Min(faces.Count, sm.GetSelectedObjectCount2(-1));
        }

        // ------------------------------------------------------------------ IPaneHost

        /// <summary>The active assembly or part (a part is studied as an assembly of its bodies).</summary>
        public bool IsOpen(string pathOrTitle)
        {
            if (string.IsNullOrEmpty(pathOrTitle) || sw == null) return false;
            foreach (var d in ((object[])sw.GetDocuments() ?? new object[0]).OfType<IModelDoc2>())
            {
                string p = d.GetPathName();
                if (string.Equals(string.IsNullOrEmpty(p) ? d.GetTitle() : p, pathOrTitle, StringComparison.OrdinalIgnoreCase)
                    && d.Visible) return true;          // an invisible document is one SolidWorks keeps for an assembly
            }
            return false;
        }

        public IModelDoc2 ActiveModel
        {
            get
            {
                var doc = sw?.ActiveDoc as IModelDoc2;
                if (doc == null) return null;
                int t = doc.GetType();
                return t == (int)swDocumentTypes_e.swDocASSEMBLY || t == (int)swDocumentTypes_e.swDocPART ? doc : null;
            }
        }

        /// <summary>The model's bounding box, metres (lo xyz, hi xyz); null when unknown.</summary>
        private static double[] ModelBox(IModelDoc2 doc)
        {
            var box = doc.GetType() == (int)swDocumentTypes_e.swDocPART
                ? ((IPartDoc)doc).GetPartBox(true) as double[]
                : ((IAssemblyDoc)doc).GetBox(0) as double[];
            return box != null && box.Length >= 6 ? box : null;
        }

        public StudyStore StoreFor(IModelDoc2 doc) { return registry.StoreFor(doc); }

        public double AssemblySizeMm(IModelDoc2 doc)
        {
            var box = ModelBox(doc);
            if (box == null) return 0;
            return Enumerable.Range(0, 3).Max(i => box[i + 3] - box[i]) * 1000.0;
        }

        public void MarkChanged(IModelDoc2 doc) { registry.MarkChanged(doc); }

        public JObj CurrentView(IModelDoc2 doc)
        {
            return Extractor.ActiveViewOrientation(doc);
        }

        public string StudyFolder(IModelDoc2 doc, string study)
        {
            CheckStudyName(study);
            string asm = doc.GetPathName();
            if (string.IsNullOrEmpty(asm)) throw new InvalidOperationException("save the document first; studies live beside it");
            return Path.Combine(Path.GetDirectoryName(asm), Path.GetFileNameWithoutExtension(asm) + "_FEA", study);
        }

        /// <summary>
        /// Export the model and the study (its face references found again) into the study
        /// folder. Overrides decided while SolidWorks was unreachable (pending_overrides.json)
        /// are merged into the stored study first (PLAN.md "Decisions made through the MCP").
        /// </summary>
        public string Export(IModelDoc2 doc, string study, out IList<string> log) { return Export(doc, study, out log, null); }

        private string Export(IModelDoc2 doc, string study, out IList<string> log, string folder)
        {
            folder = folder ?? StudyFolder(doc, study);
            var store = registry.StoreFor(doc);
            var data = store.Get(study);
            var messages = new List<string>();
            string pending = Path.Combine(folder, "pending_overrides.json");
            if (File.Exists(pending))
            {
                var po = (JObj)Json.Parse(File.ReadAllText(pending, Encoding.UTF8));
                data = data ?? store.GetOrCreate(study);
                var ovs = StudyStore.List(data, "overrides");
                foreach (JObj o in ((po["overrides"] as List<object>) ?? new List<object>()).OfType<JObj>())
                {
                    ovs.RemoveAll(x => (string)((JObj)x)["id"] == (string)o["id"]);
                    ovs.Add(o);
                }
                File.Move(pending, Path.Combine(folder, "pending_overrides.merged-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".json"));
                registry.MarkChanged(doc);
                messages.Add("merged decisions from pending_overrides.json into the study (save the assembly to keep them)");
            }
            var ex = new Extractor(sw, Version);
            string path = ex.Export(study, folder, data);
            if (data == null)
                messages.Add("no study in this assembly yet: model.json only (set loads and fixtures in the AsmFEA task pane)");
            else
            {
                // keep the refreshed references (new persistent refs after a re-match); they reach
                // the file with the user's next save, like any other study change
                var resolved = ex.ResolvedStudy;
                if (resolved != null && Json.Write(resolved) != Json.Write(data)) store.Set(study, resolved);
                messages.Add("study read from the assembly (" + registry.LoadedHow(doc) + ")");
            }
            messages.AddRange(ex.Log);
            messages.AddRange(registry.Log);
            registry.Log.Clear();
            log = messages;
            return path;
        }

        public string ModelSignature(IModelDoc2 doc) { return Extractor.ModelSignature(doc); }

        public JObj FaceRefFromSelection(IModelDoc2 doc, IFace2 face, string id)
        {
            return new Extractor(sw, Version).FaceRefFromSelection(doc, face, id);
        }

        public int HighlightFaces(IModelDoc2 doc, JObj study, IEnumerable<string> faceIds)
        {
            return SelectFaceRefs(doc, StudyStore.List(study, "face_refs"), faceIds.ToList(), false);
        }

        public void SelectComponents(IModelDoc2 doc, IEnumerable<string> partIds)
        {
            selecting = true;
            try { SelectComponentsNow(doc, partIds); }
            finally { selecting = false; }
        }

        private void SelectComponentsNow(IModelDoc2 doc, IEnumerable<string> partIds)
        {
            doc.ClearSelection2(true);
            if (doc.GetType() == (int)swDocumentTypes_e.swDocPART)
            {
                // a part document: its bodies are the "parts"; select them by id
                var mine = Extractor.BodiesOfPart(doc);
                foreach (var id in partIds)
                    foreach (var b in mine.Where(b => b.Id == id)) b.Body.Select2(true, null);
                doc.GraphicsRedraw2();
                return;
            }
            var comps = ((object[])((IAssemblyDoc)doc).GetComponents(false) ?? new object[0]).OfType<IComponent2>().ToList();
            foreach (var id in partIds)
            {
                var bits = id.Split(new[] { "/body:" }, StringSplitOptions.None);
                var comp = comps.FirstOrDefault(c => c.Name2 == bits[0]);
                if (comp == null) continue;
                // a body of a weldment: select just that body, so it can be found among the part's others
                if (bits.Length > 1 && SelectBody(comp, bits[1])) continue;
                comp.Select4(true, null, false);
            }
            doc.GraphicsRedraw2();
        }

        public void ZoomToSelection(IModelDoc2 doc)
        {
            try
            {
                if (((ISelectionMgr)doc.SelectionManager).GetSelectedObjectCount2(-1) > 0) doc.ViewZoomToSelection();
            }
            catch { }                                   // a zoom that fails leaves the view as it was
        }

        private static bool SelectBody(IComponent2 comp, string name)
        {
            try
            {
                object info;
                var all = comp.GetBodies3((int)swBodyType_e.swSolidBody, out info) as object[];
                var body = all?.OfType<IBody2>().FirstOrDefault(b => b.Name == name);
                return body != null && body.Select2(true, null);
            }
            catch { return false; }
        }

        /// <summary>The selection as the analysis names it (D98): each component or body as its part id(s), each face as a
        /// face_ref (its part, fingerprint and persistent reference), each edge or vertex as its component.</summary>
        public IList<SelItem> SelectionItems(IModelDoc2 doc)
        {
            var found = new List<SelItem>();
            if (doc == null) return found;
            var sm = (ISelectionMgr)doc.SelectionManager;
            bool isPart = doc.GetType() == (int)swDocumentTypes_e.swDocPART;
            for (int i = 1; i <= sm.GetSelectedObjectCount2(-1); i++)
            {
                object o = sm.GetSelectedObject6(i, -1);
                try
                {
                    var face = o as IFace2;
                    // an edge stands for the narrower of its two faces (a sheet's edge: its thickness face), so an edge
                    // can be joined to an edge or a face, as a weld line (the user, 2026-10-16)
                    var edge = o as IEdge;
                    if (face == null && edge != null)
                        face = ((object[])edge.GetTwoAdjacentFaces2() ?? new object[0]).OfType<IFace2>()
                               .OrderBy(f => f.GetArea()).FirstOrDefault();
                    if (face != null)
                    {
                        var fr = FaceRefFromSelection(doc, face, "SEL" + i);
                        found.Add(new SelItem((string)fr["part"], fr));
                        continue;
                    }
                    var body = o as IBody2;
                    if (isPart)
                    {
                        var mine = Extractor.BodiesOfPart(doc);
                        if (body != null) found.AddRange(mine.Where(b => b.Body.Name == body.Name).Select(b => new SelItem(b.Id)));
                        else if (mine.Count == 1) found.Add(new SelItem(mine[0].Id));
                        continue;
                    }
                    var comp = o as IComponent2 ?? sm.GetSelectedObjectsComponent4(i, -1) as IComponent2;   // LIVE-T10
                    if (comp == null) continue;
                    if (body != null)
                    {
                        var hit = Extractor.BodiesOf(comp).FirstOrDefault(b => b.Body.Name == body.Name);
                        found.Add(new SelItem(hit != null ? hit.Id : comp.Name2));
                    }
                    else found.Add(new SelItem(comp.Name2));
                }
                catch (Exception e) { StudyPane.Trace("selection item " + i + " not read: " + e.Message); }
            }
            return found;
        }

        public string ShowColours(IModelDoc2 doc, IList<JObj> sides, ICollection<string> rigidParts, bool zoom)
        {
            if (colours == null || doc == null) return "no document";
            colourArgs = Tuple.Create(doc, sides, rigidParts);
            selecting = true;
            try { return colours.Show(doc, sides, rigidParts, zoom); }
            finally { selecting = false; }
        }

        public void ClearColours()
        {
            colourArgs = null;
            try { colours?.Clear(); } catch (Exception e) { StudyPane.Trace("colour view: " + e.Message); }
        }

        public IList<IFace2> SelectedFaces(IModelDoc2 doc)
        {
            var sm = (ISelectionMgr)doc.SelectionManager;
            var faces = new List<IFace2>();
            for (int i = 1; i <= sm.GetSelectedObjectCount2(-1); i++)
            {
                var f = sm.GetSelectedObject6(i, -1) as IFace2;
                if (f != null) faces.Add(f);
            }
            return faces;
        }

        // The arrow shown for the selected load: temporary bodies, displayed but never part of the
        // document (IBody2.Display3), so nothing is modified or saved. Kept referenced until hidden.
        private readonly List<IBody2> arrowBodies = new List<IBody2>();
        private IModelDoc2 arrowDoc;
        private const int ArrowColour = 0x0000FF;                               // COLORREF (0xBBGGRR): red

        public string ShowArrow(IModelDoc2 doc, double[] tipMm, double[] direction, bool markTip)
        {
            foreach (var b in arrowBodies)
                try { b.Hide(arrowDoc); } catch { }                                // the document may have closed
            arrowBodies.Clear();
            foreach (var m in arrowManips)
                try { m.Remove(); } catch { }
            arrowManips.Clear();
            if (arrowDoc != null && arrowDoc != doc) try { arrowDoc.GraphicsRedraw2(); } catch { }
            arrowDoc = null;
            if (doc == null || direction == null) { if (doc != null) doc.GraphicsRedraw2(); return null; }
            string step = "assembly size";
            try
            {
                // size from the assembly: 15 % of its bounding-box diagonal (metres, as the API works)
                var box = ModelBox(doc);
                if (box == null) throw new InvalidOperationException("no bounding box");
                var lo = Vec3.From(box); var hi = Vec3.From(box, 3);
                double len = Math.Max(0.15 * (hi - lo).Length, 0.005), r = len * 0.035;
                double headLen = len * 0.3, headR = r * 2.6;
                var d = Vec3.From(direction).Unit();
                var tip = tipMm != null ? Vec3.From(tipMm) * 0.001 : (lo + hi) * 0.5;
                // SolidWorks' own drag arrow (as when moving a component): temporary bodies shown with
                // Display3 were accepted but never drawn in a SolidWorks 2026 assembly (found live)
                try
                {
                    step = "drag arrow";
                    // a remote force's arrow starts at its marked point and points away along the force
                    // (asked for live); otherwise the arrow points in, its tip on the faces
                    DragArrow(doc, markTip ? tip : tip - d * len, d, len);
                    if (markTip)
                    {
                        step = "point marker";
                        PointBall(doc, tip);
                    }
                    arrowDoc = doc;
                    doc.GraphicsRedraw2();
                    StudyPane.Trace("arrow: " + arrowManips.Count + " drag arrows, length " + len.ToString("G4") + " m, tip "
                                    + tip.X.ToString("G5") + "," + tip.Y.ToString("G5") + "," + tip.Z.ToString("G5") + " m");
                    return null;
                }
                catch (Exception e)
                {
                    StudyPane.Trace("drag arrow failed at " + step + ": " + e.Message + "; trying temporary bodies");
                    foreach (var m in arrowManips)
                        try { m.Remove(); } catch { }
                    arrowManips.Clear();
                }

                step = "modeler";
                var modeler = (IModeler)sw.GetModeler();

                var headBase = tip - d * headLen;
                var shaftBase = tip - d * len;
                step = "shaft";
                Show(doc, Cylinder(modeler, shaftBase, d, r, len - headLen), ArrowColour, step);
                step = "head";
                Show(doc, Cone(modeler, headBase, d, headR, headR * 0.05, headLen), ArrowColour, step);
                arrowDoc = doc;
                doc.GraphicsRedraw2();
                StudyPane.Trace("arrow: box " + string.Join(",", box.Select(x => x.ToString("G4"))) + " m, length " + len.ToString("G4")
                                + " m, tip " + tip.X.ToString("G5") + "," + tip.Y.ToString("G5") + "," + tip.Z.ToString("G5") + " m, " + arrowBodies.Count + " bodies shown");
                return null;
            }
            catch (Exception e)
            {
                // the arrow is a convenience: a failure leaves the faces highlighted, and the pane says why
                return step + ": " + e.Message;
            }
        }

        private readonly List<IManipulator> arrowManips = new List<IManipulator>();
        private ArrowHandler arrowHandler;

        /// <summary>A fixed drag arrow from <paramref name="tail"/> (metres) along <paramref name="dir"/>; not draggable.</summary>
        private void DragArrow(IModelDoc2 doc, Vec3 tail, Vec3 dir, double length)
        {
            if (arrowHandler == null) arrowHandler = new ArrowHandler();
            var m = ((IModelViewManager)doc.ModelViewManager).CreateManipulator((int)swManipulatorType_e.swDragArrowManipulator, arrowHandler) as IManipulator;
            if (m == null) throw new InvalidOperationException("SolidWorks made no drag arrow");
            var a = (IDragArrowManipulator)m.GetSpecificManipulator();
            var mu = (IMathUtility)sw.GetMathUtility();
            a.Origin = (MathPoint)mu.CreatePoint(tail.ToArray());
            a.Direction = (MathVector)mu.CreateVector(dir.ToArray());
            a.FixedLength = true;
            a.Length = length;
            a.ShowRuler = false;
            a.AllowFlip = false;
            a.ShowOppositeDirection = false;
            a.Update();
            m.Show(doc);
            arrowManips.Add(m);
        }

        /// <summary>
        /// A marker at <paramref name="at"/> (metres): a triad manipulator showing its origin ball and axes, so a
        /// remote force's point of action is marked (temporary bodies are not drawn, see above).
        /// </summary>
        private void PointBall(IModelDoc2 doc, Vec3 at)
        {
            if (arrowHandler == null) arrowHandler = new ArrowHandler();
            var m = ((IModelViewManager)doc.ModelViewManager).CreateManipulator((int)swManipulatorType_e.swTriadManipulator, arrowHandler) as IManipulator;
            if (m == null) throw new InvalidOperationException("SolidWorks made no point marker");
            var t = (ITriadManipulator)m.GetSpecificManipulator();
            t.Origin = (MathPoint)((IMathUtility)sw.GetMathUtility()).CreatePoint(at.ToArray());
            // the ball and the coloured axes (the ball alone was hard to see, its colour changing
            // with the view; found live); the planes and rings are hidden
            t.DoNotShow = (int)(swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowXYPlane
                              | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowYZPlane | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowZXPlane
                              | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowXYRING | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowYZRING
                              | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowZXRING);
            t.UpdatePosition();
            m.Show(doc);
            arrowManips.Add(m);
        }

        private void Show(IModelDoc2 doc, IBody2 body, int colour, string what)
        {
            if (body == null) throw new InvalidOperationException("SolidWorks made no " + what + " body");
            var shown = body.Display3(doc, colour, (int)swTempBodySelectOptions_e.swTempBodySelectOptionNone);
            StudyPane.Trace("Display3(" + what + ") -> " + shown);
            arrowBodies.Add(body);
        }

        // CreateBodyFromCyl / CreateBodyFromCone take [point, axis, radius(es), length]; whether the
        // point is the base or the middle is not stated clearly, so each body is moved until its
        // centroid sits where it should (LIVE-T7, confirmed 2026-10-03: the point is the BASE).
        private IBody2 Cylinder(IModeler modeler, Vec3 basePt, Vec3 axis, double radius, double length)
        {
            var b = modeler.CreateBodyFromCyl(new[] { basePt.X, basePt.Y, basePt.Z, axis.X, axis.Y, axis.Z, radius, length }) as IBody2;
            return Place(b, basePt + axis * (length / 2));
        }

        // a truncated cone (a pointed one may be refused): its centroid is h (R² + 2Rr + 3r²) / (4 (R² + Rr + r²)) up from the base
        private IBody2 Cone(IModeler modeler, Vec3 basePt, Vec3 axis, double baseRadius, double topRadius, double height)
        {
            var b = modeler.CreateBodyFromCone(new[] { basePt.X, basePt.Y, basePt.Z, axis.X, axis.Y, axis.Z, baseRadius, topRadius, height }) as IBody2;
            double R = baseRadius, q = topRadius;
            return Place(b, basePt + axis * (height * (R * R + 2 * R * q + 3 * q * q) / (4 * (R * R + R * q + q * q))));
        }

        private IBody2 Place(IBody2 b, Vec3 centroid)
        {
            if (b == null) return null;
            var mp = (double[])b.GetMassProperties(1.0);
            var shift = centroid - Vec3.From(mp);
            if (shift.Length > 1e-9)
            {
                var mu = (IMathUtility)sw.GetMathUtility();
                var xf = (MathTransform)mu.CreateTransform(new double[] { 1, 0, 0, 0, 1, 0, 0, 0, 1, shift.X, shift.Y, shift.Z, 1, 0, 0, 0 });
                b.ApplyTransform(xf);
            }
            return b;
        }

        // ------------------------------------------------------------------ helpers

        private IModelDoc2 RequireAssembly()
        {
            if (sw == null)
                throw new COMException("AsmFEA is not enabled in this SOLIDWORKS version (choose it in the AsmFEA installer)");
            var doc = ActiveModel;
            if (doc == null) throw new COMException("AsmFEA: the active document is not an assembly or a part");
            if (string.IsNullOrEmpty(doc.GetPathName()))
                throw new COMException("AsmFEA: save the document first; studies live beside it");
            return doc;
        }

        private static void CheckStudyName(string study)
        {
            if (string.IsNullOrWhiteSpace(study) || study.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                throw new COMException("AsmFEA: invalid study name '" + study + "'");
        }

        // ------------------------------------------------------------------ COM registration
        // regasm /codebase AsmFea.AddIn.dll (as administrator) calls these. HKLM lists the
        // add-in for SolidWorks; HKCU makes it load at start-up.

        private const string ADDIN_KEY = @"SOFTWARE\SolidWorks\Addins\{77cf8ee6-9213-44db-8379-945fd7578fc2}";
        private const string STARTUP_KEY = @"Software\SolidWorks\AddInsStartup\{77cf8ee6-9213-44db-8379-945fd7578fc2}";

        [ComRegisterFunction]
        public static void RegisterFunction(Type t)
        {
            using (var k = Registry.LocalMachine.CreateSubKey(ADDIN_KEY))
            {
                k.SetValue(null, 0);
                k.SetValue("Description", "Assembly Level FEA: export the neutral model for the AsmFEA pipeline");
                k.SetValue("Title", TITLE);
            }
            using (var k = Registry.CurrentUser.CreateSubKey(STARTUP_KEY))
                k.SetValue(null, 1, RegistryValueKind.DWord);
        }

        [ComUnregisterFunction]
        public static void UnregisterFunction(Type t)
        {
            Registry.LocalMachine.DeleteSubKey(ADDIN_KEY, false);
            Registry.CurrentUser.DeleteSubKey(STARTUP_KEY, false);
        }
    }

    /// <summary>
    /// The drag arrows that show a load only display: SolidWorks requires a handler for a
    /// manipulator, and every callback here ignores the user's interaction.
    /// </summary>
    [ComVisible(true)]
    public class ArrowHandler : SwManipulatorHandler2
    {
        public void OnDirectionFlipped(object pManipulator) { }
        public bool OnHandleLmbSelected(object pManipulator) { return false; }
        public void OnHandleSelected(object pManipulator, int handleIndex) { }
        public void OnUpdateDrag(object pManipulator, int handleIndex, object newPosMathPt) { }
        public void OnEndDrag(object pManipulator, int handleIndex) { }
        public void OnEndNoDrag(object pManipulator, int handleIndex) { }
        public void OnHandleRmbSelected(object pManipulator, int handleIndex) { }
        public bool OnDelete(object pManipulator) { return false; }
        public bool OnDoubleValueChanged(object pManipulator, int id, ref double value) { return false; }
        public bool OnStringValueChanged(object pManipulator, int id, ref string value) { return false; }
        public void OnItemSetFocus(object pManipulator, int id) { }
    }
}
