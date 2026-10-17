// The colour view (D98): the faces of the connections selected on the Connections tab red, rigid parts blue, every
// other part see-through. SolidWorks has no colour that is only shown (temporary bodies are not drawn in a 2026
// assembly, found live), so these are real appearances: set at the ASSEMBLY's level (components, assembly-context
// faces; never in a part document of an assembly) and put back exactly (ColourState) when the view is cleared,
// before every save, when the document closes or another becomes active, and from a journal after a crash.
// The user accepted this exception to "the add-in never modifies the document" (2026-10-16); the document stays
// marked modified after a view (no API clears the flag) and the changes may show in its undo list.
// API points only a live SolidWorks can confirm are tagged LIVE-T11..T15 (addin/LIVE-TESTS.md).
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using AsmFea.Extract;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;

namespace AsmFea.AddIn
{
    /// <summary>The document's looks: "c:" components of an assembly, "b:" bodies of a part document.</summary>
    internal class SwLooks : ILookTarget
    {
        private const int THIS = (int)swInConfigurationOpts_e.swThisConfiguration;
        private readonly Dictionary<string, IComponent2> comps = new Dictionary<string, IComponent2>();
        private readonly Dictionary<string, IBody2> bodies = new Dictionary<string, IBody2>();
        private readonly IModelDoc2 doc;
        private readonly string appearance;   // a SolidWorks appearance file the face colours start from

        public SwLooks(IModelDoc2 doc, string appearanceFile)
        {
            this.doc = doc;
            appearance = appearanceFile;
            if (doc.GetType() == (int)swDocumentTypes_e.swDocPART)
            {
                foreach (var b in ((object[])((IPartDoc)doc).GetBodies2((int)swBodyType_e.swSolidBody, true) ?? new object[0]).OfType<IBody2>())
                    bodies["b:" + b.Name] = b;
            }
            else
            {
                foreach (var c in ((object[])((IAssemblyDoc)doc).GetComponents(false) ?? new object[0]).OfType<IComponent2>())
                    comps["c:" + c.Name2] = c;
            }
        }

        public IEnumerable<string> TopKeys(IModelDoc2 doc)
        {
            if (bodies.Count > 0) return bodies.Keys.ToList();
            // top-level components only: an appearance on a sub-assembly shows on everything in it (LIVE-T11)
            return ((object[])((IAssemblyDoc)doc).GetComponents(true) ?? new object[0]).OfType<IComponent2>()
                   .Where(c => !c.IsSuppressed()).Select(c => "c:" + c.Name2).ToList();
        }

        public bool Has(string key) { return comps.ContainsKey(key) || bodies.ContainsKey(key); }

        public double[] GetLook(string key)
        {
            IComponent2 c; IBody2 b;
            if (comps.TryGetValue(key, out c))
            {
                // the component's own look, else its part's (a component with no appearance of its own)    LIVE-T11
                var own = c.HasMaterialPropertyValues() ? c.GetMaterialPropertyValues2(THIS, null) as double[] : null;
                return own ?? c.GetModelMaterialPropertyValues(c.ReferencedConfiguration) as double[];
            }
            if (bodies.TryGetValue(key, out b)) return b.MaterialPropertyValues2 as double[];
            return null;
        }

        public void SetLook(string key, double[] look)
        {
            IComponent2 c; IBody2 b;
            if (comps.TryGetValue(key, out c)) c.SetMaterialPropertyValues2(look, THIS, null);
            else if (bodies.TryGetValue(key, out b)) b.MaterialPropertyValues2 = look;
        }

        public void RemoveLook(string key)
        {
            IComponent2 c; IBody2 b;
            if (comps.TryGetValue(key, out c)) c.RemoveMaterialProperty2(THIS, null);
            else if (bodies.TryGetValue(key, out b)) b.RemoveMaterialProperty(THIS, null);
        }

        /// <summary>
        /// The faces in one appearance of the document's own display state (IModelDocExtension render material): an
        /// assembly keeps it, as SolidWorks does for an appearance applied to a face at the assembly level. A face's own
        /// SetMaterialPropertyValues2 went into the part documents (found live on a 90-part chassis, 2026-10-16). LIVE-T12
        /// </summary>
        public object AddFaceColour(IList<object> faces, double[] look)
        {
            if (appearance == null || faces.Count == 0) return null;
            var rm = doc.Extension.CreateRenderMaterial(appearance) as RenderMaterial;
            if (rm == null) return null;
            int rgb = (int)Math.Round(look[0] * 255) | (int)Math.Round(look[1] * 255) << 8 | (int)Math.Round(look[2] * 255) << 16;
            rm.PrimaryColor = rgb;                                                         // COLORREF 0xBBGGRR
            int added = 0;
            foreach (var f in faces)
                if (rm.AddEntity(f)) added++;
            int id1 = -1, id2 = -1;
            bool ok = added > 0 && doc.Extension.AddDisplayStateSpecificRenderMaterial(rm, (int)swDisplayStateOpts_e.swThisDisplayState,
                                                                                    null, out id1, out id2);
            StudyPane.Trace("colour view: face appearance " + appearance + ": " + added + " of " + faces.Count
                            + " faces taken, added " + ok + " (ids " + id1 + ", " + id2 + ")");
            return ok ? new[] { id1, id2 } : null;
        }

        public void RemoveFaceColour(object handle)
        {
            var ids = handle as int[];
            if (ids == null) return;
            doc.Extension.DeleteDisplayStateSpecificRenderMaterial(new[] { ids[0] }, new[] { ids[1] });
        }

        /// <summary>A plain appearance file of this SolidWorks (the colour is set over it), or null.</summary>
        public static string AppearanceFile(ISldWorks sw)
        {
            try
            {
                string root = sw.GetExecutablePath();
                if (root != null && root.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) root = Path.GetDirectoryName(root);
                string dir = Path.Combine(root ?? "", "data", "graphics", "Materials");
                string plain = Path.Combine(dir, "legacy", "plastics", "miscellaneous", "red plastic.p2m");
                if (File.Exists(plain)) return plain;
                return Directory.Exists(dir) ? Directory.EnumerateFiles(dir, "*plastic*.p2m", SearchOption.AllDirectories).FirstOrDefault() : null;
            }
            catch { return null; }
        }
    }

    /// <summary>One document's colour view.</summary>
    public class ColourView
    {
        /// <summary>How close the view zooms to the patches shown (the user, 2026-10-17): 0 the whole model fitted to the
        /// window, ZoomMax right on the patches' faces, between them in even steps (default 2). Kept for the user in
        /// HKCU\Software\AsmFEA, AutoZoom (a new name: ZoomLevel counted the other way).</summary>
        public static int ZoomLevel
        {
            get
            {
                if (zoomLevel == null)
                {
                    zoomLevel = 2;
                    try
                    {
                        using (var k = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(@"Software\AsmFEA"))
                            if (k?.GetValue("AutoZoom") is int v) zoomLevel = Math.Max(0, Math.Min(ZoomMax, v));
                    }
                    catch (Exception) { }
                }
                return zoomLevel.Value;
            }
            set
            {
                zoomLevel = value;
                try
                {
                    using (var k = Microsoft.Win32.Registry.CurrentUser.CreateSubKey(@"Software\AsmFEA"))
                        k.SetValue("AutoZoom", value, Microsoft.Win32.RegistryValueKind.DWord);
                }
                catch (Exception) { }
            }
        }
        private static int? zoomLevel;

        /// <summary>The slider's top level: right on the faces.</summary>
        public const int ZoomMax = 6;

        private readonly ISldWorks sw;
        private IModelDoc2 doc;
        private SwLooks looks;
        private ColourState state;
        // what this SolidWorks refused this session (a colour that reached a part document, LIVE-T11/T12): not tried again,
        // so no more parts are marked modified; the faces are selected instead
        private static bool viewRefused;
        private readonly List<string> faceRefs = new List<string>();   // persistent refs of the coloured faces (journal)

        public ColourView(ISldWorks sw) { this.sw = sw; }

        public bool IsOn { get { return state != null && (state.On || outline != null || balls.Count > 0); } }
        public IModelDoc2 Doc { get { return doc; } }

        /// <summary>
        /// Show the faces of ``sides`` ({part, fingerprint}: connections.json sides) red and the rigid parts blue, the
        /// rest see-through. Returns null when shown in colour, else why not (the faces are then selected instead).
        /// </summary>
        public string Show(IModelDoc2 d, IList<JObj> sides, ICollection<string> rigidParts, bool zoom = true)
        {
            if (d == null) return "no document";
            if (doc != null && !SameDoc(doc, d)) Clear();
            if (doc == null)
            {
                doc = d;
                looks = new SwLooks(d, SwLooks.AppearanceFile(sw));
                state = new ColourState(looks);
            }
            var view = d.ActiveView as IModelView;
            bool isPart = d.GetType() == (int)swDocumentTypes_e.swDocPART;
            var clock = System.Diagnostics.Stopwatch.StartNew();
            var took = new List<string>();                // each step's time, in the log (the user asked for speed, 2026-10-17)
            Action<string> lap = what => { took.Add(what + " " + clock.ElapsedMilliseconds + " ms"); clock.Restart(); };
            try
            {
                if (view != null) view.EnableGraphicsUpdate = false;                                           // LIVE-T14
                // each connection's patch: the smaller of its two faces (the footprint when a small face sits on a big one)
                var patches = new List<FaceInfo>();
                var why = new List<string>();
                foreach (var grp in GroupSides(sides))
                {
                    var found = grp.Select(sd => FindPartFace(d, (string)sd["part"], sd["fingerprint"] as JObj)).ToList();
                    for (int i = 0; i < found.Count; i++) if (found[i] == null) why.Add((string)grp[i]["part"]);
                    var best = found.Where(f => f != null).OrderBy(f => f.Area).FirstOrDefault();
                    if (best != null) patches.Add(best);
                }
                lap("faces");
                // rigid parts: their component (a part's own component) or body (a part document) blue
                var rigidKeys = new List<string>();
                foreach (var pid in rigidParts ?? new string[0])
                {
                    var bits = pid.Split(new[] { "/body:" }, StringSplitOptions.None);
                    if (isPart) { if (bits.Length > 1 && looks.Has("b:" + bits[1])) rigidKeys.Add("b:" + bits[1]); }
                    else if (bits.Length == 1 && looks.Has("c:" + bits[0])) rigidKeys.Add("c:" + bits[0]);
                }
                // nothing to colour and nothing coloured: no component walk, part-document check or journal (D122)
                bool journalled = !viewRefused && (rigidKeys.Count > 0 || state.On);
                if (journalled)
                {
                    // only the connected parts see-through (the user's choice, 2026-10-16), so the patch between them shows;
                    // the rest as it is
                    var mine = new HashSet<string>(sides.Select(sd => (isPart ? "b:" + (((string)sd["part"]).Split(new[] { "/body:" }, StringSplitOptions.None).Last())
                                                                             : "c:" + ((string)sd["part"]).Split('/')[0])));
                    var see = looks.TopKeys(d).Where(k => !rigidKeys.Contains(k) && mine.Contains(k)).ToList();
                    var parts = isPart ? new List<IModelDoc2>() : PartDocs(d, see.Concat(rigidKeys));
                    var wasDirty = parts.ToDictionary(p => p, p => SafeDirty(p));
                    Journal(d, see.Concat(rigidKeys));
                    // every part see-through, rigid ones blue (component level); the guard (LIVE-T11) after it
                    // no see-through any more: the balls show through solid parts (the user, 2026-10-16); rigid parts blue
                    state.ApplyLooks(new string[0], rigidKeys);
                    var touched = parts.Where(p => !wasDirty[p] && SafeDirty(p)).ToList();
                    if (touched.Count > 0)
                    {
                        viewRefused = true;
                        state.Clear();
                        StudyPane.Trace("colour view: refused, component appearances changed part " + touched[0].GetTitle());
                        why.Insert(0, "(see-through refused: it changed part " + touched[0].GetTitle() + "; do not save that part)");
                    }
                }
                // the patches outlined in red by a temporary 3-D sketch of the assembly (the user's idea, 2026-10-16): drawn
                // over the see-through parts, where a face colour lay hidden between the two parts and under the selection
                // highlight. LIVE-T12
                lap("looks");
                int lines = 0;                    // the outline sketch is no longer drawn: the balls mark the patch
                int balls = PlaceBalls(d, patches);
                lap("balls");
                if (zoom) { ZoomTo(d, patches); lap("zoom"); }
                if (journalled) Journal(d, null);
                StudyPane.Trace("colour view: " + patches.Count + " patch(es) outlined in " + lines + " red lines and " + balls + " balls, " + rigidKeys.Count
                                + " rigid" + (viewRefused ? ", see-through off" : "") + "; " + string.Join(", ", took));
                return why.Count > 0 ? "not found: the faces of " + string.Join(", ", why.Distinct()) : null;
            }
            catch (Exception e)
            {
                StudyPane.Trace("colour view failed: " + e);
                try { Clear(); } catch { }
                return "the colours could not be set (" + e.Message + ")";
            }
            finally
            {
                // the one redraw of the whole update (the zoom no longer redraws on its own, D122)
                var redraw = System.Diagnostics.Stopwatch.StartNew();
                if (view != null) view.EnableGraphicsUpdate = true;
                try { d.GraphicsRedraw2(); } catch { }
                StudyPane.Trace("colour view: redraw " + redraw.ElapsedMilliseconds + " ms");
            }
        }

        /// <summary>Everything back as it was; safe to call at any time.</summary>
        public void Clear()
        {
            if (state == null) return;
            var view = doc?.ActiveView as IModelView;
            try
            {
                if (view != null) view.EnableGraphicsUpdate = false;
                state.Clear();
                RemoveOutline(doc);
                faceRefs.Clear();
                cache.Clear();
                Forget(doc);
            }
            catch (Exception e) { StudyPane.Trace("colour view: clear failed: " + e.Message); }
            finally
            {
                try { if (view != null) view.EnableGraphicsUpdate = true; doc?.GraphicsRedraw2(); } catch { }
                state = null;
                looks = null;
                doc = null;
            }
        }

        // ------------------------------------------------------------------ finding faces

        /// <summary>A part's faces: (body entry, part face, surface type, area mm2), and each one's fingerprint once worked out.</summary>
        private class FaceInfo { public Extractor.BodyEntry B; public IFace2 Pf; public string Surface; public double Area; public JObj Fp; }
        private readonly Dictionary<string, List<FaceInfo>> cache = new Dictionary<string, List<FaceInfo>>();

        /// <summary>The assembly-context face of a part (component or body id) that matches a fingerprint. Only faces of the
        /// same type and about the same area get a fingerprint (its fine tessellation took seconds a click on big parts).</summary>
        private FaceInfo FindPartFace(IModelDoc2 d, string partId, JObj fp)
        {
            if (fp == null) return null;
            List<FaceInfo> all;
            if (!cache.TryGetValue(partId, out all))
            {
                all = new List<FaceInfo>();
                foreach (var b in BodyEntries(d, partId))
                    foreach (var pf in ((object[])b.Body.GetFaces() ?? new object[0]).OfType<IFace2>())
                        all.Add(new FaceInfo { B = b, Pf = pf, Surface = Extractor.SurfaceType((ISurface)pf.GetSurface()),
                                               Area = pf.GetArea() * Units.M2_TO_MM2 });
                cache[partId] = all;
            }
            double area = Convert.ToDouble(fp["area"]);
            var cands = all.Where(f => f.Surface == (string)fp["surface"] && Math.Abs(f.Area - area) <= Rematch.AREA_RTOL * area).ToList();
            foreach (var f in cands)
                if (f.Fp == null) f.Fp = Extractor.Fingerprint(f.B.Body, f.Pf, f.B.X);
            string reason;
            int k = Rematch.Pick(cands.Select(c => Rematch.Score(fp, c.Fp)).ToList(), out reason);
            return k < 0 ? null : cands[k];
        }

        /// <summary>The sides by connection: resolve writes two per connection, in order.</summary>
        private static List<List<JObj>> GroupSides(IList<JObj> sides)
        {
            var out_ = new List<List<JObj>>();
            for (int i = 0; i < sides.Count; i += 2)
                out_.Add(sides.Skip(i).Take(2).ToList());
            return out_;
        }

        // ------------------------------------------------------------------ the red outline (a temporary 3-D sketch)

        public const string OUTLINE_NAME = "AsmFEA connection (temporary)";
        private const int MAX_LINES = 6000;          // a patch of many faces is drawn up to this many lines
        private IFeature outline;

        /// <summary>Each face's edges (part frame, through its component's transform) as red lines of one 3-D sketch of
        /// the document. Returns the number of lines drawn.</summary>
        private int DrawOutline(IModelDoc2 d, List<FaceInfo> faces)
        {
            RemoveOutline(d);
            var segs = new List<double[]>();
            foreach (var f in faces)
                foreach (var e in ((object[])f.Pf.GetEdges() ?? new object[0]).OfType<IEdge>())
                {
                    var pts = EdgePoints(e).Select(q => f.B.X.Point(new Vec3(q[0] * Units.M_TO_MM, q[1] * Units.M_TO_MM, q[2] * Units.M_TO_MM))).ToList();
                    for (int i = 0; i + 1 < pts.Count && segs.Count < MAX_LINES; i++)
                        segs.Add(new[] { pts[i].X / 1000, pts[i].Y / 1000, pts[i].Z / 1000, pts[i + 1].X / 1000, pts[i + 1].Y / 1000, pts[i + 1].Z / 1000 });
                }
            if (segs.Count == 0) return 0;
            var skm = d.SketchManager;
            bool db = skm.AddToDB;
            try
            {
                skm.AddToDB = true;                  // no snapping or inferred relations: the points exactly
                skm.DisplayWhenAdded = false;
                skm.Insert3DSketch(true);
                var sketch = skm.ActiveSketch;
                foreach (var g in segs)
                {
                    var seg = skm.CreateLine(g[0], g[1], g[2], g[3], g[4], g[5]) as ISketchSegment;
                    if (seg == null) continue;
                    try { seg.Color = 0x0000FF; seg.Width = 3; } catch { }     // COLORREF red; a heavier line
                }
                skm.Insert3DSketch(true);            // close it
                outline = sketch as IFeature;
                if (outline != null)
                {
                    try { outline.Name = OUTLINE_NAME; } catch { }
                    // the sketch's own colour (Sketch Color in its menu): a segment's colour shows only while the sketch
                    // is edited, otherwise grey (found live, 2026-10-16). LIVE-T12
                    try
                    {
                        outline.SetMaterialPropertyValues2(ColourState.Look(ColourState.RED, 0.0),
                                                           (int)swInConfigurationOpts_e.swThisConfiguration, null);
                    }
                    catch (Exception e) { StudyPane.Trace("colour view: sketch colour not set: " + e.Message); }
                }
            }
            finally
            {
                skm.DisplayWhenAdded = true;
                skm.AddToDB = db;
                d.ClearSelection2(true);
            }
            return segs.Count;
        }

        /// <summary>Points along an edge, metres, part frame: its ends for a line, else 24 steps along it.</summary>
        private static List<double[]> EdgePoints(IEdge e)
        {
            var out_ = new List<double[]>();
            var c = e.GetCurve() as ICurve;
            var cp = e.GetCurveParams3();
            if (c == null || cp == null) return out_;
            if (c.IsLine())
            {
                out_.Add((double[])cp.StartPoint);
                out_.Add((double[])cp.EndPoint);
                return out_;
            }
            double u0 = cp.UMinValue, u1 = cp.UMaxValue;
            for (int i = 0; i <= 24; i++)
            {
                var v = c.Evaluate2(u0 + (u1 - u0) * i / 24.0, 0) as double[];
                if (v != null && v.Length >= 3) out_.Add(new[] { v[0], v[1], v[2] });
            }
            return out_;
        }

        // ------------------------------------------------------------------ red balls round the patches (manipulators)

        // at most BALLS balls in all, spread over the patches; with more connections than that, one at each patch's centre
        // (drawing a ball takes a moment: a selected part's dozens of connections were slow, found live 2026-10-16)
        private const int BALLS = 9, MAX_BALLS = 120;
        // every ball made for the document, all shown: those a click does not need sit on top of one it does, so no ball
        // is hidden or shown again (D123: showing one takes about 175 ms, moving one nothing, measured live)
        private readonly List<IManipulator> balls = new List<IManipulator>();
        private IModelDoc2 ballDoc;                   // the document the balls belong to
        private ArrowHandler ballHandler;

        /// <summary>A ball shows only its origin.</summary>
        private static readonly int ShowOrigin = (int)(swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowXAxis | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowYAxis
                             | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowZAxis | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowXYPlane
                             | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowYZPlane | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowZXPlane
                             | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowXYRING | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowYZRING
                             | swTriadManipulatorDoNotShow_e.swTriadManipulatorDoNotShowZXRING);

        /// <summary>The balls are those of ``d``: another document's are removed (they went with it).</summary>
        private void BallsFor(IModelDoc2 d)
        {
            if (ballDoc != null && !SameDoc(ballDoc, d)) RemoveAllBalls();
            ballDoc = d;
        }

        private IManipulator MakeBall(IModelDoc2 d, MathPoint origin)
        {
            if (ballHandler == null) ballHandler = new ArrowHandler();
            var mgr = (IModelViewManager)d.ModelViewManager;
            var m = mgr.CreateManipulator((int)swManipulatorType_e.swTriadManipulator, ballHandler) as IManipulator;
            if (m == null) return null;
            var t = (ITriadManipulator)m.GetSpecificManipulator();
            t.Origin = origin;
            t.DoNotShow = ShowOrigin;
            try { t.SetColorRefAtIndex((int)swTriadManipulatorControlPoints_e.swTriadManipulatorOrigin, 0x0000FF); } catch { }
            t.UpdatePosition();
            m.Show(d);
            return m;
        }

        /// <summary>Red balls along each patch's outline: triad manipulators showing only their origin, coloured red. A
        /// manipulator is drawn over everything and is no part of the document (as the load arrows, found live 2026-10-03).
        /// The balls already shown are moved; only when a click needs more than there are is one made; the ones it does
        /// not need sit on its first ball. LIVE-T12, LIVE-T28</summary>
        private int PlaceBalls(IModelDoc2 d, List<FaceInfo> faces)
        {
            BallsFor(d);
            var mu = (IMathUtility)sw.GetMathUtility();
            var places = new List<Vec3>();
            foreach (var f in faces)
            {
                // points evenly along the face's outline, and its centre
                var ring = new List<Vec3>();
                foreach (var e in ((object[])f.Pf.GetEdges() ?? new object[0]).OfType<IEdge>())
                    ring.AddRange(EdgePoints(e).Select(q => f.B.X.Point(new Vec3(q[0] * Units.M_TO_MM, q[1] * Units.M_TO_MM, q[2] * Units.M_TO_MM))));
                if (ring.Count == 0) continue;
                var c = new Vec3(ring.Average(v => v.X), ring.Average(v => v.Y), ring.Average(v => v.Z));
                var pick = new List<Vec3> { c };
                int per = faces.Count >= BALLS ? 1 : Math.Max(1, BALLS / faces.Count);
                // the rest spread round the outline: each the point furthest from those chosen (no clusters)
                while (pick.Count < per)
                {
                    var far = ring.OrderByDescending(q => pick.Min(pp => (q - pp).Length)).First();
                    if (pick.Min(pp => (far - pp).Length) < 1.0) break;          // mm: nothing far enough left
                    pick.Add(far);
                }
                places.AddRange(pick);
                if (places.Count >= MAX_BALLS) break;
            }
            if (places.Count > MAX_BALLS) places = places.Take(MAX_BALLS).ToList();
            if (places.Count == 0) return 0;
            // each ball to a place; spare balls onto the first place (out of sight under that ball)
            var kept = new List<IManipulator>();
            for (int i = 0; i < Math.Max(places.Count, balls.Count); i++)
            {
                var at = places[i < places.Count ? i : 0];
                var origin = (MathPoint)mu.CreatePoint(new[] { at.X / 1000, at.Y / 1000, at.Z / 1000 });
                if (i < balls.Count)
                {
                    try
                    {
                        var t = (ITriadManipulator)balls[i].GetSpecificManipulator();
                        t.Origin = origin;
                        t.UpdatePosition();
                        kept.Add(balls[i]);
                        continue;
                    }
                    catch { try { balls[i].Remove(); } catch { } }       // could not be moved: a new one, if needed
                }
                if (i >= places.Count) continue;
                var m = MakeBall(d, origin);
                if (m != null) kept.Add(m);
            }
            balls.Clear();
            balls.AddRange(kept);
            return Math.Min(places.Count, balls.Count);
        }

        /// <summary>The view zoomed to the patches shown, with room round them: the faces selected, zoomed to (as Show in
        /// SolidWorks does for problems), zoomed out a step, and the selection cleared again (its highlight would cover the
        /// markers). ViewZoomTo2's box sent the view off the screen (found live, 2026-10-16).</summary>
        private void ZoomTo(IModelDoc2 d, List<FaceInfo> faces)
        {
            if (faces.Count == 0) return;
            var sm = (ISelectionMgr)d.SelectionManager;
            var kept = new List<object>();
            for (int i = 1; i <= sm.GetSelectedObjectCount2(-1); i++) kept.Add(sm.GetSelectedObject6(i, -1));
            var view = d.ActiveView as IModelView;
            // SolidWorks animates each zoom (Options > View > View animation speed): about half a second a click, found in
            // the pane log (2026-10-17). Off for these steps only, and the user's setting put back at once (D122)
            double anim = -1;
            try
            {
                anim = sw.GetUserPreferenceDoubleValue((int)swUserPreferenceDoubleValue_e.swViewAnimationSpeed);
                if (anim > 0) sw.SetUserPreferenceDoubleValue((int)swUserPreferenceDoubleValue_e.swViewAnimationSpeed, 0);
            }
            catch { anim = -1; }
            try
            {
                // far left: the whole model, SolidWorks' own zoom to fit (the user, 2026-10-17). Its scale, needed for the
                // span, is remembered for the document and view direction: fitting a big assembly is slow (D122)
                string key = FitKey(d, view);
                double fit;
                if (ZoomLevel == 0 || key == null || !fitScale.TryGetValue(key, out fit))
                {
                    d.ViewZoomtofit2();
                    fit = view != null ? view.Scale2 : 0;
                    if (key != null && fit > 0) fitScale[key] = fit;
                }
                if (ZoomLevel > 0)
                {
                    d.ClearSelection2(true);
                    foreach (var f in faces)
                    {
                        var asm = f.B.Comp == null ? f.Pf : f.B.Comp.GetCorresponding(f.Pf) as IFace2;
                        if (asm != null) ((IEntity)asm).Select4(true, null);
                    }
                    if (((ISelectionMgr)d.SelectionManager).GetSelectedObjectCount2(-1) > 0)
                    {
                        d.ViewZoomToSelection();
                        // between the faces and the whole model: SolidWorks' own zoom-out steps, which keep the view
                        // centred (setting the view scale zoomed about another point and the model left the screen,
                        // found live). How many steps span the two ends is measured on this model: the scale's ratio
                        // over one step's.
                        if (ZoomLevel < ZoomMax)
                        {
                            double close = view != null ? view.Scale2 : 0;
                            int done = 0;
                            if (stepRatio <= 1 && view != null)                 // SolidWorks' zoom-out step, measured once
                            {
                                d.ViewZoomout();
                                done = 1;
                                double one = view.Scale2;
                                if (one > 0 && one < close) stepRatio = close / one;
                            }
                            int span = 12;                                      // without a readable scale
                            if (fit > 0 && close > fit && stepRatio > 1)
                                span = (int)Math.Round(Math.Log(close / fit) / Math.Log(stepRatio));
                            int steps = (int)Math.Round((ZoomMax - ZoomLevel) * Math.Max(1, span) / (double)ZoomMax);
                            for (int i = done; i < steps; i++) d.ViewZoomout();
                        }
                    }
                }
            }
            catch (Exception e) { StudyPane.Trace("colour view: zoom failed: " + e.Message); }
            finally
            {
                if (anim > 0)
                    try { sw.SetUserPreferenceDoubleValue((int)swUserPreferenceDoubleValue_e.swViewAnimationSpeed, anim); } catch { }
                // what the user had selected, selected again (the user, 2026-10-16: a part picked to join was lost)
                try
                {
                    d.ClearSelection2(true);
                    foreach (var o in kept)
                        try { ((IEntity)o).Select4(true, null); } catch { try { (o as IComponent2)?.Select4(true, null, false); } catch { } }
                }
                catch { }
            }
        }

        // the whole-model scale per document and view direction, and SolidWorks' zoom-out step (D122)
        private static readonly Dictionary<string, double> fitScale = new Dictionary<string, double>();
        private static double stepRatio;

        /// <summary>The document and the view's direction, as a key for its whole-model scale; null when unknown.</summary>
        private static string FitKey(IModelDoc2 d, IModelView view)
        {
            try
            {
                var o = view?.Orientation3 as MathTransform;
                var a = o?.ArrayData as double[];
                if (a == null) return null;
                return d.GetPathName() + "|" + d.GetTitle() + "|" + string.Join(",", a.Take(9).Select(x => Math.Round(x, 3).ToString(System.Globalization.CultureInfo.InvariantCulture)));
            }
            catch { return null; }
        }

        /// <summary>Every ball removed (the colour view cleared, another document, the add-in unloaded): quick, measured
        /// live.</summary>
        public void RemoveAllBalls()
        {
            foreach (var m in balls)
                try { m.Remove(); } catch { }
            balls.Clear();
            ballDoc = null;
        }

        private void RemoveOutline(IModelDoc2 d)
        {
            RemoveAllBalls();
            if (outline == null || d == null) { outline = null; return; }
            try
            {
                d.ClearSelection2(true);
                if (outline.Select2(false, 0)) d.Extension.DeleteSelection2(0);
            }
            catch (Exception e) { StudyPane.Trace("colour view: outline not removed: " + e.Message); }
            outline = null;
        }

        /// <summary>A view's outline sketch left in a document (a crash), found by its name.</summary>
        private static void RemoveOutlineByName(IModelDoc2 d)
        {
            var f = d.FirstFeature() as IFeature;
            while (f != null)
            {
                var next = f.GetNextFeature() as IFeature;
                if (f.Name == OUTLINE_NAME)
                {
                    d.ClearSelection2(true);
                    if (f.Select2(false, 0)) d.Extension.DeleteSelection2(0);
                }
                f = next;
            }
        }

        /// <summary>The components owning faces, as look keys (their part documents are watched by the guard).</summary>
        private static IEnumerable<string> OwnerKeys(IEnumerable<IFace2> faces)
        {
            foreach (var f in faces)
            {
                IComponent2 c = null;
                try { c = ((IEntity)f).GetComponent() as IComponent2; } catch { }
                if (c != null) yield return "c:" + c.Name2;
            }
        }

        private static IEnumerable<IFace2> FacesOfBody(IModelDoc2 d, string partId)
        {
            foreach (var b in BodyEntries(d, partId))
                foreach (var pf in ((object[])b.Body.GetFaces() ?? new object[0]).OfType<IFace2>())
                {
                    var f = b.Comp == null ? pf : b.Comp.GetCorresponding(pf) as IFace2;
                    if (f != null) yield return f;
                }
        }

        private static IEnumerable<Extractor.BodyEntry> BodyEntries(IModelDoc2 d, string partId)
        {
            if (d.GetType() == (int)swDocumentTypes_e.swDocPART)
                return Extractor.BodiesOfPart(d).Where(b => b.Id == partId);
            string comp = partId.Split(new[] { "/body:" }, StringSplitOptions.None)[0];
            var c = ((object[])((IAssemblyDoc)d).GetComponents(false) ?? new object[0]).OfType<IComponent2>().FirstOrDefault(x => x.Name2 == comp);
            return c == null ? Enumerable.Empty<Extractor.BodyEntry>() : Extractor.BodiesOf(c).Where(b => b.Id == partId);
        }

        private static void SelectFaces(IModelDoc2 d, IEnumerable<IFace2> faces)
        {
            d.ClearSelection2(true);
            foreach (var f in faces) ((IEntity)f).Select4(true, null);
        }

        private static List<IModelDoc2> PartDocs(IModelDoc2 d, IEnumerable<string> keys)
        {
            var want = new HashSet<string>(keys.Where(k => k.StartsWith("c:")).Select(k => k.Substring(2)));
            var out_ = new List<IModelDoc2>();
            foreach (var c in ((object[])((IAssemblyDoc)d).GetComponents(false) ?? new object[0]).OfType<IComponent2>())
            {
                if (!want.Any(w => c.Name2 == w || c.Name2.StartsWith(w + "/"))) continue;
                var m = c.GetModelDoc2() as IModelDoc2;
                if (m != null && !out_.Any(x => ReferenceEquals(x, m))) out_.Add(m);
            }
            return out_;
        }

        private static bool SafeDirty(IModelDoc2 d) { try { return d.GetSaveFlag(); } catch { return false; } }

        private static bool SameDoc(IModelDoc2 a, IModelDoc2 b)
        {
            try { return ReferenceEquals(a, b) || string.Equals(a.GetPathName(), b.GetPathName(), StringComparison.OrdinalIgnoreCase) && a.GetTitle() == b.GetTitle(); }
            catch { return false; }
        }

        // ------------------------------------------------------------------ the journal (a crash leaves no colours behind)

        private static string JournalPath()
        {
            string dir = Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.LocalApplicationData), "AsmFEA");
            Directory.CreateDirectory(dir);
            return Path.Combine(dir, "colourview.json");
        }

        private static JObj ReadJournal()
        {
            try
            {
                string p = JournalPath();
                return File.Exists(p) ? (Json.Parse(File.ReadAllText(p, Encoding.UTF8)) as JObj ?? new JObj()) : new JObj();
            }
            catch { return new JObj(); }
        }

        private static void WriteJournal(JObj j)
        {
            try { File.WriteAllText(JournalPath(), Json.Write(j), Encoding.UTF8); } catch { }
        }

        private static string DocKey(IModelDoc2 d) { string p = d.GetPathName(); return string.IsNullOrEmpty(p) ? d.GetTitle() : p; }

        /// <summary>Record the looks about to change (each key's look before the view) and the faces coloured.</summary>
        private void Journal(IModelDoc2 d, IEnumerable<string> keys)
        {
            var j = ReadJournal();
            var e = j[DocKey(d)] as JObj ?? new JObj { { "looks", new JObj() }, { "faces", new List<object>() } };
            var lk = e["looks"] as JObj ?? new JObj();
            foreach (var k in keys ?? new string[0])
                if (!lk.Has(k))
                {
                    var was = looks.GetLook(k);
                    lk[k] = was == null ? null : (object)was.Cast<object>().ToList();
                }
            e["looks"] = lk;
            e["faces"] = faceRefs.Distinct().Cast<object>().ToList();
            j[DocKey(d)] = e;
            WriteJournal(j);
        }

        private static void Forget(IModelDoc2 d)
        {
            if (d == null) return;
            var j = ReadJournal();
            if (j.Remove(DocKey(d))) WriteJournal(j);
        }

        /// <summary>Put back a colour view a crash left behind in this document (its journal entry), if any.</summary>
        public static bool RestoreLeftovers(IModelDoc2 d)
        {
            if (d == null) return false;
            var j = ReadJournal();
            var e = j[DocKey(d)] as JObj;
            if (e == null) return false;
            try
            {
                var lk = new SwLooks(d, null);
                foreach (var kv in (e["looks"] as JObj) ?? new JObj())
                {
                    if (!lk.Has(kv.Key)) continue;
                    var was = (kv.Value as List<object>)?.Select(x => Convert.ToDouble(x)).ToArray();
                    lk.RemoveLook(kv.Key);
                    if (was != null && !ColourState.SameLook(lk.GetLook(kv.Key), was)) lk.SetLook(kv.Key, was);
                }
                foreach (var r in ((e["faces"] as List<object>) ?? new List<object>()).OfType<string>())
                {
                    int err;
                    var f = d.Extension.GetObjectByPersistReference3(Convert.FromBase64String(r), out err) as IFace2;
                    f?.RemoveMaterialProperty2((int)swInConfigurationOpts_e.swThisConfiguration, null);
                }
                RemoveOutlineByName(d);
                StudyPane.Trace("colour view: put back the colours left from an earlier session in " + d.GetTitle());
            }
            catch (Exception ex) { StudyPane.Trace("colour view: leftovers not restored: " + ex.Message); }
            Forget(d);
            return true;
        }
    }
}
