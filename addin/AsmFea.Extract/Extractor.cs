// Extraction: the active assembly -> model.json + one STEP per unique part and configuration.
//
// Rules (PLAN.md "SolidWorks add-in"): resolved, unsuppressed components only; a missing
// material is an exception, never a default; the user's documents are never rebuilt,
// modified or saved (STEP export uses Save As Copy, which leaves the document untouched).
//
// Frames. Bodies, faces and descriptors are read from the PART document, so they are in the
// part frame without doubt; the component transform takes them to the assembly frame.
// Assembly-context faces (mate entities) are traced back to the part faces through
// IComponent2.GetCorresponding. Items to confirm on the first live run are tagged LIVE-Tn and
// listed in addin/LIVE-TESTS.md.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;

namespace AsmFea.Extract
{
    public class ExtractException : Exception
    {
        public ExtractException(string message) : base(message) { }
    }

    public partial class Extractor
    {
        public const string SCHEMA_VERSION = "1.1";   // 1.1: welds, part subassembly
        /// <summary>Custom property a user sets on a part (or configuration) to force its kind.</summary>
        public const string KIND_PROPERTY = "AsmFEA_Kind";

        private readonly ISldWorks sw;
        private readonly string addinVersion;
        private readonly List<string> log = new List<string>();

        public IList<string> Log { get { return log; } }

        public Extractor(ISldWorks sw, string addinVersion)
        {
            this.sw = sw;
            this.addinVersion = addinVersion;
        }

        // one body of one component (or of the part document itself): the unit the pipeline calls a "part"
        public class BodyEntry
        {
            public string Id;
            public IComponent2 Comp;     // null when the document exported is a part
            public string CompName;      // the component's name, or the part document's name
            public IBody2 Body;          // part-document body (part frame)
            public Xform X;              // part frame -> assembly frame, mm
            public string StepRel;
        }

        private readonly List<BodyEntry> bodies = new List<BodyEntry>();
        private readonly List<JObj> faceRefs = new List<JObj>();
        private readonly List<JObj> issues = new List<JObj>();

        /// <summary>
        /// Write model.json and parts/*.step into `folder` for the active assembly or part. Returns
        /// the path of model.json. Throws ExtractException when there is nothing sensible to write
        /// (no active assembly or part); everything else becomes an issue or a missing material.
        /// A part document is exported as an assembly of its own bodies: no mates, the part frame
        /// as the assembly frame.
        /// </summary>
        public string Export(string study, string folder, JObj studyData = null)
        {
            try { return ExportModel(folder, studyData); }
            catch (Exception e) when (!(e is ExtractException))
            {
                throw new ExtractException("failed at " + stage + ": " + e.Message);
            }
        }

        private string ExportModel(string folder, JObj studyData)
        {
            var doc = sw.ActiveDoc as IModelDoc2;
            bool isPart = doc != null && doc.GetType() == (int)swDocumentTypes_e.swDocPART;
            if (doc == null || (!isPart && doc.GetType() != (int)swDocumentTypes_e.swDocASSEMBLY))
                throw new ExtractException("the active document is not an assembly or a part");
            Directory.CreateDirectory(Path.Combine(folder, "parts"));

            var materials = new MaterialLibrary();
            LoadMaterialLibraries(materials);
            var matsOut = new Dictionary<string, JObj>();
            var parts = new List<JObj>();
            var exported = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);   // part+config -> step rel path

            if (isPart)
            {
                // the part itself: its bodies are the "parts", in the part's own frame
                string name = PartDocName(doc);
                string config = doc.ConfigurationManager.ActiveConfiguration.Name;
                try { ExportBodies(doc, config, name, null, Xform.Identity(), folder, materials, matsOut, parts, exported); }
                catch (Exception e)
                {
                    issues.Add(Issue("EXTRACT_ITEM_FAILED", null, new JObj {
                        { "item", "part " + name }, { "step", stage }, { "error", e.Message } }));
                    log.Add("FAILED at " + stage + ": " + e.Message);
                }
            }
            else
            {
                var asm = (IAssemblyDoc)doc;
                // Lightweight components carry no bodies. Resolving loads them in memory only; the
                // file is not changed (PLAN.md asks for this explicitly).
                asm.ResolveAllLightWeightComponents(false);
                var comps = ((object[])asm.GetComponents(false) ?? new object[0]).Cast<IComponent2>()
                            .OrderBy(c => c.Name2, StringComparer.Ordinal).ToList();
                foreach (var comp in comps)
                {
                    // one component's failure is reported and the rest still export
                    try { ExportComponent(comp, folder, materials, matsOut, parts, exported); }
                    catch (Exception e)
                    {
                        issues.Add(Issue("EXTRACT_ITEM_FAILED", null, new JObj {
                            { "item", "component " + SafeName(comp) }, { "step", stage }, { "error", e.Message } }));
                        log.Add("FAILED at " + stage + ": " + e.Message);
                    }
                }
            }

            // a part has no mates, and its touching bodies are joined by the resolver from their geometry
            stage = "mates";
            var mates = isPart ? new List<JObj>() : Mates(doc);
            stage = "weld beads";
            var welds = isPart ? new List<JObj>() : Welds(doc);
            // the study's loads and fixtures: their faces found again (persistent reference, then
            // fingerprint) and written as study.json beside the model
            if (studyData != null)
            {
                stage = "study face references";
                var resolved = ResolveStudy(doc, studyData);
                File.WriteAllText(Path.Combine(folder, "study.json"), Json.Write(resolved), new UTF8Encoding(false));
                ResolvedStudy = resolved;
            }
            var model = new JObj
            {
                { "schema_version", SCHEMA_VERSION },
                { "source", new JObj {
                    { "assembly", doc.GetPathName() },
                    { "configuration", doc.ConfigurationManager.ActiveConfiguration.Name },
                    { "sw_version", sw.RevisionNumber() },
                    { "addin_version", addinVersion },
                    { "timestamp", DateTime.Now.ToString("yyyy-MM-ddTHH:mm:sszzz", CultureInfo.InvariantCulture) },
                    { "view", ActiveViewOrientation(doc) } } },
                { "materials", matsOut.Values.ToList() },
                { "parts", parts },
                { "mates", mates },
                { "face_refs", faceRefs },
            };
            if (welds.Count > 0) model["welds"] = welds;
            if (issues.Count > 0) model["issues"] = issues;
            string path = Path.Combine(folder, "model.json");
            File.WriteAllText(path, Json.Write(model), new UTF8Encoding(false));
            log.Add(string.Format(CultureInfo.InvariantCulture, "RESULT: {0} parts, {1} materials, {2} STEP files, {3} face references, {4} issues -> {5}",
                                  parts.Count, matsOut.Count, exported.Count, faceRefs.Count, issues.Count, path));
            return path;
        }

        /// <summary>
        /// The user's view at export, for the report's plots (the user picks the orientation by
        /// how they look at the model). ModelView.Orientation3 maps model to view coordinates
        /// in SolidWorks' row-vector layout, so screen axis i is (a[i], a[3+i], a[6+i]) in the
        /// model: right, up, and toward the viewer (LIVE-M8b). Null when there is no view.
        /// </summary>
        public static JObj ActiveViewOrientation(IModelDoc2 doc)
        {
            try
            {
                var view = doc.ActiveView as IModelView;
                var a = view == null ? null : view.Orientation3.ArrayData as double[];
                if (a == null || a.Length < 9) return null;
                var up = new Vec3(a[1], a[4], a[7]);
                var toward = new Vec3(a[2], a[5], a[8]);
                if (up.Length < 1e-9 || toward.Length < 1e-9) return null;
                return new JObj { { "toward_viewer", toward.Unit().ToRounded(9) }, { "up", up.Unit().ToRounded(9) } };
            }
            catch { return null; }
        }

        /// <summary>What the extractor is doing now: named in every failure.</summary>
        private string stage = "start";

        private static string SafeName(IComponent2 c)
        {
            try { return c.Name2; } catch { return "(unnamed)"; }
        }

        /// <summary>A part document's name in model.json: its file name without the extension
        /// (the title, for a part not yet saved).</summary>
        public static string PartDocName(IModelDoc2 doc)
        {
            string path = doc.GetPathName();
            string name = string.IsNullOrEmpty(path) ? doc.GetTitle() : Path.GetFileNameWithoutExtension(path);
            return name.Replace("/", "_");
        }

        /// <summary>The solid bodies of a part document, with the ids model.json uses (part frame).</summary>
        public static List<BodyEntry> BodiesOfPart(IModelDoc2 pdoc)
        {
            var part = pdoc as IPartDoc;
            if (part == null) return new List<BodyEntry>();
            string name = PartDocName(pdoc);
            var pb = ((object[])part.GetBodies2((int)swBodyType_e.swSolidBody, true) ?? new object[0]).Cast<IBody2>().ToList();
            return pb.Select(b => new BodyEntry { Id = pb.Count == 1 ? name : name + "/body:" + b.Name,
                                                  Comp = null, CompName = name, Body = b, X = Xform.Identity() }).ToList();
        }

        private void ExportComponent(IComponent2 comp, string folder, MaterialLibrary materials,
                                     Dictionary<string, JObj> matsOut, List<JObj> parts, Dictionary<string, string> exported)
        {
            stage = comp.Name2 + ": component";
            if (comp.IsSuppressed() || comp.IsEnvelope()) return;
            var pdoc = comp.GetModelDoc2() as IModelDoc2;
            if (pdoc == null || pdoc.GetType() != (int)swDocumentTypes_e.swDocPART) return;   // sub-assemblies: their parts are listed too
            if (IsInSuppressedParent(comp)) return;
            string config = comp.ReferencedConfiguration;
            var x = Xform.FromSolidWorks((double[])comp.Transform2.ArrayData);
            if (x.OrthoError() > 1e-6)
                throw new ExtractException("component " + comp.Name2 + " has a mirrored or scaled transform");
            ExportBodies(pdoc, config, comp.Name2, comp, x, folder, materials, matsOut, parts, exported);
        }

        /// <summary>One part's STEP file and the descriptors of its bodies: a component of the
        /// assembly (comp, its transform), or the part document itself (comp null, identity).</summary>
        private void ExportBodies(IModelDoc2 pdoc, string config, string compName, IComponent2 comp, Xform x, string folder,
                                  MaterialLibrary materials, Dictionary<string, JObj> matsOut, List<JObj> parts,
                                  Dictionary<string, string> exported)
        {
            var part = (IPartDoc)pdoc;
            stage = compName + ": STEP export";
            string key = pdoc.GetPathName() + "|" + config;
            string stepRel;
            if (!exported.TryGetValue(key, out stepRel))
            {
                stepRel = ExportStep(pdoc, config, folder, exported.Values, compName);
                exported[key] = stepRel;
            }

            // no STEP written (configuration not active, or the export failed): the issue already
            // says so, and descriptors of the wrong configuration would only add noise
            if (!File.Exists(Path.Combine(folder, stepRel.Replace('/', Path.DirectorySeparatorChar)))) return;

            stage = compName + ": bodies";
            var pbodies = ((object[])part.GetBodies2((int)swBodyType_e.swSolidBody, true) ?? new object[0]).Cast<IBody2>().ToList();
            string kindOverride = PartProperty(pdoc, config, KIND_PROPERTY);
            foreach (var body in pbodies)
            {
                string id = pbodies.Count == 1 ? compName : compName + "/body:" + body.Name;
                var entry = new BodyEntry { Id = id, Comp = comp, CompName = compName, Body = body, X = x, StepRel = stepRel };
                bodies.Add(entry);

                stage = compName + " body " + body.Name + ": material";
                string db;
                string mat = body.GetMaterialPropertyName(config, out db);
                if (string.IsNullOrEmpty(mat)) mat = part.GetMaterialPropertyName2(config, out db);
                mat = mat ?? "";
                if (mat != "" && !matsOut.ContainsKey(mat))
                {
                    var props = materials.Find(mat, db);
                    if (props != null && props.Complete) matsOut[mat] = props.ToJson();
                    else log.Add("material '" + mat + "' has no " + (props == null ? "library entry" :
                                 string.Join(", ", props.MissingProperties())) + ": its parts are reported as MATERIAL_MISSING");
                }
                stage = compName + " body " + body.Name + ": descriptors";
                parts.Add(PartJson(entry, pdoc, mat, kindOverride, stepRel));
            }
        }

        private static bool IsInSuppressedParent(IComponent2 comp)
        {
            for (var p = comp.GetParent() as IComponent2; p != null; p = p.GetParent() as IComponent2)
                if (p.IsSuppressed()) return true;
            return false;
        }

        private void LoadMaterialLibraries(MaterialLibrary lib)
        {
            foreach (var path in ((object[])sw.GetMaterialDatabases() ?? new object[0]).Cast<string>())
            {
                try { lib.LoadFile(path); }
                catch (Exception e) { log.Add("material library " + path + " unreadable: " + e.Message); }
            }
        }

        private static string PartProperty(IModelDoc2 pdoc, string config, string name)
        {
            foreach (var cfg in new[] { config, "" })
            {
                var mgr = pdoc.Extension.CustomPropertyManager[cfg];
                if (mgr == null) continue;
                string val, resolved;
                bool wasResolved, linked;
                mgr.Get6(name, false, out val, out resolved, out wasResolved, out linked);
                if (!string.IsNullOrWhiteSpace(resolved)) return resolved.Trim().ToLowerInvariant();
            }
            return null;
        }

        // ------------------------------------------------------------------ STEP

        private string ExportStep(IModelDoc2 pdoc, string config, string folder, IEnumerable<string> taken, string component)
        {
            string baseName = Sanitise(Path.GetFileNameWithoutExtension(pdoc.GetPathName()) + "_" + config);
            string rel = "parts/" + baseName + ".step";
            for (int n = 2; taken.Contains(rel, StringComparer.OrdinalIgnoreCase); n++)
                rel = "parts/" + baseName + "_" + n + ".step";
            string active = pdoc.ConfigurationManager.ActiveConfiguration.Name;
            if (!string.Equals(active, config, StringComparison.Ordinal))
            {
                // Activating it would modify the part's state; the user does it (D16).
                issues.Add(Issue("STEP_CONFIG_INACTIVE", new[] { component }, new JObj {
                    { "file", Path.GetFileName(pdoc.GetPathName()) }, { "configuration", config }, { "active", active } }));
                return rel;
            }
            string full = Path.Combine(folder, rel.Replace('/', Path.DirectorySeparatorChar));
            int err = 0, warn = 0;
            // IModelDocExtension.SaveAs: in every supported version (SaveAs3 arrived in SolidWorks 2019, D26)
            bool ok = pdoc.Extension.SaveAs(full, (int)swSaveAsVersion_e.swSaveAsCurrentVersion,
                                            (int)(swSaveAsOptions_e.swSaveAsOptions_Silent | swSaveAsOptions_e.swSaveAsOptions_Copy),
                                            null, ref err, ref warn);
            if (!ok || !File.Exists(full))
                issues.Add(Issue("STEP_EXPORT_FAILED", new[] { component }, new JObj {
                    { "file", Path.GetFileName(pdoc.GetPathName()) }, { "configuration", config },
                    { "error", "SaveAs error " + err + ", warnings " + warn } }));
            return rel;
        }

        private static string Sanitise(string s)
        {
            var bad = Path.GetInvalidFileNameChars();
            var sb = new StringBuilder();
            foreach (char c in s) sb.Append(bad.Contains(c) || c == ' ' ? '_' : c);
            return sb.ToString();
        }

        // ------------------------------------------------------------------ parts

        private JObj PartJson(BodyEntry e, IModelDoc2 pdoc, string material, string kindOverride, string stepRel)
        {
            var body = e.Body;
            var mp = (double[])body.GetMassProperties(1.0);    // centre xyz, volume, area, mass, ... (SI)
            var centroid = new Vec3(mp[0], mp[1], mp[2]) * Units.M_TO_MM;
            double volume = mp[3] * Units.M3_TO_MM3;
            Vec3 lo, hi;
            TightBox(body, out lo, out hi);

            string kind = Classify(e, pdoc, kindOverride);
            var o = new JObj
            {
                { "id", e.Id },
                { "kind", kind },
                { "material", material },
                { "step", stepRel },
                { "body", new JObj { { "volume", Math.Round(volume, 6) }, { "centroid", centroid.ToRounded() },
                                     { "bbox_min", lo.ToRounded() }, { "bbox_max", hi.ToRounded() } } },
                { "geometry_hash", GeometryHash(body, volume, centroid, lo, hi) },
                { "transform", e.X.ToRowMajor4x4().Select(v => Math.Round(v, 9)).ToArray() },
                { "fixed", e.Comp != null && e.Comp.IsFixed() },
            };
            // a part inside a sub-assembly: the sub-assembly is treated as rigid (one multi-body part)
            string group = e.Comp == null ? null : Subassembly(e.Comp);
            if (group != null) o["subassembly"] = group;
            if (kind == "member")
            {
                var m = MemberData(e);
                if (m == null) { o["kind"] = "solid"; }
                else o["member"] = m;
            }
            else if (kind == "sheet") o["sheet"] = SheetData(body);
            else if (kind == "fastener")
            {
                var f = FastenerData(body, pdoc);
                if (f == null) o["kind"] = "solid";
                else o["fastener"] = f;
            }
            return o;
        }

        /// <summary>Exact box from extreme points (IBody2.GetBodyBox may be loose).</summary>
        private static void TightBox(IBody2 body, out Vec3 lo, out Vec3 hi)
        {
            double x, y, z;
            double[] l = new double[3], h = new double[3];
            for (int k = 0; k < 3; k++)
            {
                double[] d = new double[3];
                d[k] = 1;
                body.GetExtremePoint(d[0], d[1], d[2], out x, out y, out z);
                h[k] = new[] { x, y, z }[k];
                body.GetExtremePoint(-d[0], -d[1], -d[2], out x, out y, out z);
                l[k] = new[] { x, y, z }[k];
            }
            lo = Vec3.From(l) * Units.M_TO_MM;
            hi = Vec3.From(h) * Units.M_TO_MM;
        }

        /// <summary>
        /// A stable hash of the body's shape: volume, area, centroid, box and each face's type and
        /// area, rounded so a rebuild that changes nothing gives the same hash. (The STEP bytes
        /// carry a timestamp, so they cannot be hashed.)
        /// </summary>
        private static string GeometryHash(IBody2 body, double volume, Vec3 c, Vec3 lo, Vec3 hi)
        {
            var sb = new StringBuilder();
            Func<double, string> r = v => Math.Round(v, 4).ToString("R", CultureInfo.InvariantCulture);
            sb.Append(r(volume)).Append('|').Append(string.Join(",", new[] { c.X, c.Y, c.Z, lo.X, lo.Y, lo.Z, hi.X, hi.Y, hi.Z }.Select(r)));
            var faces = ((object[])body.GetFaces() ?? new object[0]).Cast<IFace2>()
                        .Select(f => SurfaceType((ISurface)f.GetSurface()) + ":" + r(f.GetArea() * Units.M2_TO_MM2))
                        .OrderBy(s => s, StringComparer.Ordinal);
            sb.Append('|').Append(string.Join(";", faces));
            using (var sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(sb.ToString()))).Replace("-", "").ToLowerInvariant().Substring(0, 16);
        }

        private static string Classify(BodyEntry e, IModelDoc2 pdoc, string kindOverride)
        {
            if (!string.IsNullOrEmpty(kindOverride))
            {
                if (new[] { "member", "sheet", "solid", "fastener", "weld_bead" }.Contains(kindOverride)) return kindOverride;
            }
            foreach (var f in ((object[])e.Body.GetFeatures() ?? new object[0]).Cast<IFeature>())
                if (f.GetTypeName2() == "WeldMemberFeat") return "member";
            if (e.Body.IsSheetMetal()) return "sheet";
            if (pdoc.Extension.ToolboxPartType != (int)swToolBoxPartType_e.swNotAToolboxPart) return "fastener";
            return "solid";
        }

        // ------------------------------------------------------------------ members

        /// <summary>
        /// Profile from the weldment profile file; axis and spans from the body itself, so the
        /// feature's selections are never accessed (AccessSelections rolls the part back).
        /// Null (and an issue) when no straight axis is found: the member is then a plain solid.
        /// </summary>
        private JObj MemberData(BodyEntry e)
        {
            var body = e.Body;
            string profilePath = null, profileConfig = null;
            foreach (var f in ((object[])body.GetFeatures() ?? new object[0]).Cast<IFeature>())
                if (f.GetTypeName2() == "WeldMemberFeat")
                {
                    var def = f.GetDefinition() as IStructuralMemberFeatureData;
                    if (def != null) { profilePath = def.WeldmentProfilePath; profileConfig = def.ConfigurationName; }
                    break;
                }
            var profile = Profiles.Parse(profilePath, profileConfig);
            var faces = ((object[])body.GetFaces() ?? new object[0]).Cast<IFace2>().ToList();
            Vec3? axisOpt = MemberAxis(faces);
            if (axisOpt == null)
            {
                issues.Add(Issue("MEMBER_PATH_UNKNOWN", new[] { e.Id }, new JObj { { "part", e.Id } }));
                return null;
            }
            var axis = axisOpt.Value;
            double x, y, z;
            body.GetExtremePoint(axis.X, axis.Y, axis.Z, out x, out y, out z);
            double sMax = new Vec3(x, y, z).Dot(axis) * Units.M_TO_MM;
            body.GetExtremePoint(-axis.X, -axis.Y, -axis.Z, out x, out y, out z);
            double sMin = new Vec3(x, y, z).Dot(axis) * Units.M_TO_MM;
            var mp = (double[])body.GetMassProperties(1.0);
            var c = new Vec3(mp[0], mp[1], mp[2]) * Units.M_TO_MM;
            var p0 = c + axis * (sMin - c.Dot(axis));
            var p1 = c + axis * (sMax - c.Dot(axis));
            double length = sMax - sMin;

            // every face that is not part of the plain extruded profile touches the length
            var touched = new List<Span>();
            foreach (var f in faces)
            {
                if (IsLateral(f, axis)) continue;
                double a = double.MaxValue, b = double.MinValue;
                var tri = (float[])f.GetTessTriangles(true);
                if (tri == null || tri.Length == 0) { touched.Add(new Span(0, length)); continue; }
                for (int i = 0; i + 2 < tri.Length; i += 3)
                {
                    double s = new Vec3(tri[i], tri[i + 1], tri[i + 2]).Dot(axis) * Units.M_TO_MM - sMin;
                    a = Math.Min(a, s); b = Math.Max(b, s);
                }
                touched.Add(new Span(a, b));
            }
            double depth = Profiles.Depth(profile, Math.Sqrt(Math.Max(1e-9, mp[3] * Units.M3_TO_MM3 / Math.Max(length, 1e-9))));
            var spans = CleanSpans.Compute(length, touched, depth);
            return new JObj
            {
                { "profile", profile },
                { "path", new JObj { { "type", "line" }, { "p0", p0.ToRounded() }, { "p1", p1.ToRounded() } } },
                { "clean_spans", spans.Select(s => new JObj { { "s0", Math.Round(s.S0, 4) }, { "s1", Math.Round(s.S1, 4) } }).ToList() },
            };
        }

        /// <summary>The member axis: the cross product of the normals of the two largest
        /// non-parallel planar faces, or the axis of the largest cylindrical face (tubes).</summary>
        private static Vec3? MemberAxis(List<IFace2> faces)
        {
            var byArea = faces.OrderByDescending(f => f.GetArea()).ToList();
            var first = byArea.FirstOrDefault();
            if (first == null) return null;
            var s0 = (ISurface)first.GetSurface();
            if (s0.IsCylinder())
            {
                var cp = (double[])s0.CylinderParams;           // origin xyz, axis xyz, radius
                return new Vec3(cp[3], cp[4], cp[5]).Unit();
            }
            if (!s0.IsPlane()) return null;
            var n0 = Vec3.From((double[])first.Normal).Unit();
            foreach (var f in byArea.Skip(1))
            {
                var s = (ISurface)f.GetSurface();
                if (!s.IsPlane()) continue;
                var n = Vec3.From((double[])f.Normal).Unit();
                var ax = n0.Cross(n);
                if (ax.Length > 0.5) return ax.Unit();   // more than 30 degrees apart
            }
            return null;
        }

        private static bool IsLateral(IFace2 f, Vec3 axis)
        {
            var s = (ISurface)f.GetSurface();
            if (s.IsPlane())
                return Math.Abs(Vec3.From((double[])f.Normal).Unit().Dot(axis)) < AddinDefaults.AXIS_PARALLEL_COS;
            if (s.IsCylinder())
            {
                var cp = (double[])s.CylinderParams;
                return Math.Abs(new Vec3(cp[3], cp[4], cp[5]).Unit().Dot(axis)) > 1 - AddinDefaults.AXIS_PARALLEL_COS;
            }
            return false;
        }

        // ------------------------------------------------------------------ sheet, fastener

        private static JObj SheetData(IBody2 body)
        {
            double t = 0;
            foreach (var f in ((object[])body.GetFeatures() ?? new object[0]).Cast<IFeature>())
            {
                if (f.GetTypeName2() != "SheetMetal") continue;
                var def = f.GetDefinition() as ISheetMetalFeatureData;
                if (def != null) { t = def.Thickness * Units.M_TO_MM; break; }
            }
            var faces = ((object[])body.GetFaces() ?? new object[0]).Cast<IFace2>().ToList();
            var biggest = faces.Where(f => ((ISurface)f.GetSurface()).IsPlane()).OrderByDescending(f => f.GetArea()).FirstOrDefault();
            bool flat = biggest != null;
            if (flat)
            {
                var n = Vec3.From((double[])biggest.Normal).Unit();
                foreach (var f in faces)
                {
                    var s = (ISurface)f.GetSurface();
                    if (s.IsPlane()) continue;
                    if (s.IsCylinder() || s.IsCone())
                    {
                        // holes and countersinks run through the thickness; a bend's axis lies in the sheet
                        double[] p = s.IsCylinder() ? (double[])s.CylinderParams : (double[])s.ConeParams;
                        if (Math.Abs(new Vec3(p[3], p[4], p[5]).Unit().Dot(n)) > 0.999) continue;
                    }
                    flat = false;
                    break;
                }
            }
            if (t <= 0) t = SheetThicknessFromFaces(body, faces);
            return new JObj { { "thickness", Math.Round(t, 6) }, { "flat", flat } };
        }

        /// <summary>Fallback when no sheet-metal feature gives the thickness: volume over half the area.</summary>
        private static double SheetThicknessFromFaces(IBody2 body, List<IFace2> faces)
        {
            var mp = (double[])body.GetMassProperties(1.0);
            double vol = mp[3] * Units.M3_TO_MM3, area = mp[4] * Units.M2_TO_MM2;
            return area > 0 ? 2 * vol / area : 1.0;
        }

        /// <summary>Nominal diameter and axis from the largest cylindrical face (the shank).</summary>
        private static JObj FastenerData(IBody2 body, IModelDoc2 pdoc)
        {
            IFace2 shank = null;
            foreach (var f in ((object[])body.GetFaces() ?? new object[0]).Cast<IFace2>())
                if (((ISurface)f.GetSurface()).IsCylinder() && (shank == null || f.GetArea() > shank.GetArea())) shank = f;
            if (shank == null) return null;
            var cp = (double[])((ISurface)shank.GetSurface()).CylinderParams;
            string type = pdoc.Extension.ToolboxPartType != (int)swToolBoxPartType_e.swNotAToolboxPart ? "toolbox" : "user";
            return new JObj
            {
                { "type", type + ":" + Path.GetFileNameWithoutExtension(pdoc.GetPathName()) },
                { "d", Math.Round(2 * cp[6] * Units.M_TO_MM, 6) },
                { "axis", new Vec3(cp[3], cp[4], cp[5]).Unit().ToRounded(9) },
            };
        }

        // ------------------------------------------------------------------ mates and faces

        private List<JObj> Mates(IModelDoc2 doc)
        {
            var mates = new List<JObj>();
            for (var feat = doc.FirstFeature() as IFeature; feat != null; feat = feat.GetNextFeature() as IFeature)
            {
                if (feat.GetTypeName2() != "MateGroup") continue;
                for (var sub = feat.GetFirstSubFeature() as IFeature; sub != null; sub = sub.GetNextSubFeature() as IFeature)
                {
                    stage = "mate " + sub.Name;
                    try { CollectMate(doc, sub, mates); }
                    catch (Exception e)
                    {
                        issues.Add(Issue("EXTRACT_ITEM_FAILED", null, new JObj {
                            { "item", "mate " + sub.Name }, { "step", stage }, { "error", e.Message } }));
                        log.Add("FAILED at " + stage + ": " + e.Message);
                    }
                }
            }
            return mates;
        }

        private void CollectMate(IModelDoc2 doc, IFeature feat, List<JObj> mates)
        {
            var mate = feat.GetSpecificFeature2() as IMate2;
            if (mate == null)
            {
                // a mate folder: walk into it
                for (var sub = feat.GetFirstSubFeature() as IFeature; sub != null; sub = sub.GetNextSubFeature() as IFeature)
                    CollectMate(doc, sub, mates);
                return;
            }
            var supp = feat.IsSuppressed2((int)swInConfigurationOpts_e.swThisConfiguration, null) as Array;
            if (supp != null && supp.Length > 0 && Convert.ToBoolean(supp.GetValue(0))) return;
            string type = MateTypeName(mate.Type);
            // A mate to the assembly's own planes, axes or origin only places a component in space; it
            // joins nothing, so it is left out (found live: a frame placed by three such mates raised six
            // "unmatched" warnings, each pinned on an arbitrary body of the weldment).
            for (int i = 0; i < mate.GetMateEntityCount(); i++)
            {
                var rc = mate.MateEntity(i).ReferenceComponent as IComponent2;
                if (rc == null || rc.IsRoot())
                {
                    log.Add("mate " + feat.Name + " places a component on the assembly's own reference geometry: left out");
                    return;
                }
                // a mate to a suppressed component joins nothing in this configuration (found live)
                if (rc.IsSuppressed())
                {
                    log.Add("mate " + feat.Name + " involves suppressed " + rc.Name2 + ": left out");
                    return;
                }
            }
            var partIds = new List<string>();
            var entities = new List<string>();
            JObj geom = null;
            for (int i = 0; i < mate.GetMateEntityCount(); i++)
            {
                var me = mate.MateEntity(i);
                var comp = me.ReferenceComponent as IComponent2;
                string fid = null, owner = null;
                if (comp != null && me.Reference is IFace2)
                    fid = FaceRefFor(doc, comp, (IFace2)me.Reference, out owner);
                if (fid != null)
                {
                    entities.Add(fid);
                    if (!partIds.Contains(owner)) partIds.Add(owner);
                }
                else
                {
                    // which body the entity belongs to is unknown: name the part when it has one body,
                    // otherwise the component (all its bodies), never an arbitrary one of them
                    var own = comp == null ? new List<BodyEntry>() : bodies.Where(b => b.CompName == comp.Name2).ToList();
                    string who = own.Count == 1 ? own[0].Id : own.Count > 1 ? comp.Name2 : null;
                    if (own.Count == 1 && !partIds.Contains(who)) partIds.Add(who);
                    issues.Add(Issue("MATE_ENTITY_UNMATCHED", who == null ? null : new[] { who }, new JObj {
                        { "mate", feat.Name }, { "entity", EntityLabel(me) } }));
                }
                if (geom == null)
                {
                    JObj fp = null;
                    if (fid != null)
                        foreach (var fr in faceRefs) if ((string)fr["id"] == fid) fp = (JObj)fr["fingerprint"];
                    geom = EntityGeometry(me, fp);
                }
            }
            if (partIds.Count == 0) return;
            var o = new JObj { { "id", feat.Name }, { "type", type }, { "parts", partIds.Take(2).ToList() } };
            if (entities.Count > 0) o["entities"] = entities;
            if (geom != null) foreach (var kv in geom) o[kv.Key] = kv.Value;
            mates.Add(o);
        }

        /// <summary>"swMateCOINCIDENT" -> "coincident": the names the resolver expects.</summary>
        public static string MateTypeName(int t)
        {
            string n = Enum.GetName(typeof(swMateType_e), t) ?? ("type" + t);
            if (n.StartsWith("swMate")) n = n.Substring(6);
            return n.ToLowerInvariant();
        }

        private static string EntityLabel(IMateEntity2 me)
        {
            var comp = me.ReferenceComponent as IComponent2;
            string kind = Enum.GetName(typeof(swSelectType_e), me.ReferenceType2) ?? "entity";   // selection type (LIVE-T2)
            return kind.Replace("swSel", "").ToLowerInvariant() + (comp != null ? " of " + comp.Name2 : "");
        }

        /// <summary>
        /// Geometry of a mate entity. For a part face it comes from the face's fingerprint
        /// (assembly frame): IMateEntity2.EntityParams did not give an assembly-frame point in
        /// SolidWorks 2026 (LIVE-T2), so it is not used. Otherwise only the kind is known, from
        /// IMateEntity2.ReferenceType (swMateEntity2ReferenceType_e; ReferenceType2 is the
        /// selection type).
        /// </summary>
        private static JObj EntityGeometry(IMateEntity2 me, JObj fingerprint)
        {
            if (fingerprint != null)
            {
                string surf = (string)fingerprint["surface"];
                var o = new JObj { { "geometry", surf == "plane" || surf == "cylinder" ? surf : "other" },
                                   { "point", fingerprint["centroid"] } };
                if (fingerprint.Has("normal")) o["direction"] = fingerprint["normal"];
                else if (fingerprint.Has("axis")) o["direction"] = fingerprint["axis"];
                if (fingerprint.Has("radius")) o["radius"] = fingerprint["radius"];
                return o;
            }
            string g;
            if (me.Reference is IRefPlane || me.Reference is IRefAxis || me.Reference is IRefPoint) g = "reference";
            else switch ((swMateEntity2ReferenceType_e)me.ReferenceType)
            {
                case swMateEntity2ReferenceType_e.swMateEntity2ReferenceType_Plane: g = "plane"; break;
                case swMateEntity2ReferenceType_e.swMateEntity2ReferenceType_Cylinder: g = "cylinder"; break;
                case swMateEntity2ReferenceType_e.swMateEntity2ReferenceType_Line: g = "line"; break;
                case swMateEntity2ReferenceType_e.swMateEntity2ReferenceType_Point: g = "point"; break;
                default: g = "other"; break;
            }
            return new JObj { { "geometry", g } };
        }

        /// <summary>
        /// The face_ref id for an assembly-context face, adding it on first use. The face is traced
        /// to its part-document face (part frame) so the fingerprint is computed there and moved
        /// to the assembly frame with the component transform.
        /// </summary>
        private string FaceRefFor(IModelDoc2 asmDoc, IComponent2 comp, IFace2 asmFace, out string owner)
        {
            owner = null;
            var hit = Trace(comp, asmFace, bodies.Where(b => b.CompName == comp.Name2));
            if (hit == null) return null;
            owner = hit.Item1.Id;
            var fr = MakeFaceRef(asmDoc, hit.Item1, hit.Item2, asmFace, "F" + (faceRefs.Count + 1));
            foreach (var old in faceRefs)
                if ((string)old["part"] == owner && fr["persistent_ref"] != null && (string)old["persistent_ref"] == (string)fr["persistent_ref"])
                    return (string)old["id"];
            faceRefs.Add(fr);
            return (string)fr["id"];
        }

        /// <summary>The body and part-document face behind an assembly-context face (LIVE-T3); with
        /// comp null (a part document) the face is a part face already, and only its body is found.</summary>
        private Tuple<BodyEntry, IFace2> Trace(IComponent2 comp, IFace2 asmFace, IEnumerable<BodyEntry> candidates)
        {
            foreach (var b in candidates)
                foreach (var pf in ((object[])b.Body.GetFaces() ?? new object[0]).Cast<IFace2>())
                {
                    var corr = comp == null ? pf : comp.GetCorresponding(pf) as IFace2;   // part face -> assembly context
                    if (corr != null && sw.IsSame(corr, asmFace).Equals((int)swObjectEquality.swObjectSame))
                        return Tuple.Create(b, pf);
                }
            return null;
        }

        private static JObj MakeFaceRef(IModelDoc2 asmDoc, BodyEntry b, IFace2 partFace, IFace2 asmFace, string id)
        {
            var bytes = asmDoc.Extension.GetPersistReference3(asmFace) as byte[];
            var feat = partFace.GetFeature() as IFeature;
            return new JObj
            {
                { "id", id }, { "part", b.Id }, { "persistent_ref", bytes == null ? null : Convert.ToBase64String(bytes) },
                { "feature", feat == null ? null : feat.Name },
                { "fingerprint", Fingerprint(b.Body, partFace, b.X) },
            };
        }

        /// <summary>The solid bodies of a part component, with the ids model.json uses.</summary>
        public static List<BodyEntry> BodiesOf(IComponent2 comp)
        {
            var part = comp.GetModelDoc2() as IPartDoc;
            if (part == null) return new List<BodyEntry>();
            var x = Xform.FromSolidWorks((double[])comp.Transform2.ArrayData);
            var pb = ((object[])part.GetBodies2((int)swBodyType_e.swSolidBody, true) ?? new object[0]).Cast<IBody2>().ToList();
            return pb.Select(b => new BodyEntry { Id = pb.Count == 1 ? comp.Name2 : comp.Name2 + "/body:" + b.Name,
                                                  Comp = comp, CompName = comp.Name2, Body = b, X = x }).ToList();
        }

        /// <summary>A study face_ref for a face selected in the assembly or part (task pane).</summary>
        public JObj FaceRefFromSelection(IModelDoc2 asmDoc, IFace2 asmFace, string id)
        {
            if (asmDoc.GetType() == (int)swDocumentTypes_e.swDocPART)
            {
                var own = Trace(null, asmFace, BodiesOfPart(asmDoc));
                if (own == null) throw new ExtractException("the selected face is not on a solid body of the part");
                return MakeFaceRef(asmDoc, own.Item1, own.Item2, asmFace, id);
            }
            var comp = ((IEntity)asmFace).GetComponent() as IComponent2;
            if (comp == null) throw new ExtractException("select faces of parts in the assembly");
            var hit = Trace(comp, asmFace, BodiesOf(comp));
            if (hit == null) throw new ExtractException("the selected face of " + comp.Name2 + " could not be traced to its part");
            return MakeFaceRef(asmDoc, hit.Item1, hit.Item2, asmFace, id);
        }

        /// <summary>The study as written beside the model by the last export (resolved face refs).</summary>
        public JObj ResolvedStudy { get; private set; }

        /// <summary>
        /// A copy of the study with every face ref found again: by persistent reference (and its
        /// fingerprint refreshed, since dimensions may have changed), else re-matched by
        /// fingerprint among the faces of the same part (FACE_REF_REMATCHED, to confirm), else
        /// reported lost (FACE_REF_LOST, blocking).
        /// </summary>
        private JObj ResolveStudy(IModelDoc2 asmDoc, JObj study)
        {
            var copy = (JObj)Json.Parse(Json.Write(study));
            var refs = StudyStore.List(copy, "face_refs");
            bool isPart = asmDoc.GetType() == (int)swDocumentTypes_e.swDocPART;
            var comps = isPart ? new List<IComponent2>()
                               : ((object[])((IAssemblyDoc)asmDoc).GetComponents(false) ?? new object[0]).Cast<IComponent2>().ToList();
            for (int i = 0; i < refs.Count; i++)
            {
                var fr = (JObj)refs[i];
                string id = (string)fr["id"], part = (string)fr["part"];
                stage = "study face " + id;
                IFace2 face = null;
                if (fr["persistent_ref"] != null)
                {
                    int err;
                    try { face = asmDoc.Extension.GetObjectByPersistReference3(Convert.FromBase64String((string)fr["persistent_ref"]), out err) as IFace2; }
                    catch { face = null; }
                }
                if (face != null)
                {
                    var comp = isPart ? null : ((IEntity)face).GetComponent() as IComponent2;
                    var hit = isPart ? Trace(null, face, BodiesOfPart(asmDoc))
                                     : comp == null ? null : Trace(comp, face, BodiesOf(comp));
                    if (hit != null)
                    {
                        var fresh = MakeFaceRef(asmDoc, hit.Item1, hit.Item2, face, id);
                        refs[i] = fresh;
                        CheckChanged(fr["fingerprint"] as JObj, hit.Item1, hit.Item2, (JObj)fresh["fingerprint"], id);
                        continue;
                    }
                }
                // re-match by fingerprint among the faces of the same part
                string compName = part.Split(new[] { "/body:" }, StringSplitOptions.None)[0];
                var owner = comps.FirstOrDefault(c => c.Name2 == compName);
                bool ownerFound = isPart ? compName == PartDocName(asmDoc) : owner != null;
                var stored = fr["fingerprint"] as JObj;
                var cands = new List<Tuple<BodyEntry, IFace2, JObj>>();
                if (ownerFound && stored != null)
                {
                    var bodiesOfPart = isPart ? BodiesOfPart(asmDoc) : BodiesOf(owner);
                    var same = bodiesOfPart.Where(b => b.Id == part).ToList();
                    foreach (var b in (same.Count > 0 ? same : bodiesOfPart))
                        foreach (var pf in ((object[])b.Body.GetFaces() ?? new object[0]).Cast<IFace2>())
                        {
                            if (SurfaceType((ISurface)pf.GetSurface()) != (string)stored["surface"]) continue;
                            cands.Add(Tuple.Create(b, pf, Fingerprint(b.Body, pf, b.X)));
                        }
                }
                string reason;
                int k = Rematch.Pick(cands.Select(c => Rematch.Score(stored, c.Item3)).ToList(), out reason);
                if (!ownerFound) reason = isPart ? "the part was renamed (the study names " + compName + ")"
                                                 : "component " + compName + " is no longer in the assembly";
                if (k < 0)
                {
                    issues.Add(Issue("FACE_REF_LOST", new[] { part }, new JObj { { "face", id }, { "part", part }, { "reason", reason } }, new[] { id }));
                    continue;
                }
                var c0 = cands[k];
                var asmFace = isPart ? c0.Item2 : owner.GetCorresponding(c0.Item2) as IFace2;
                refs[i] = MakeFaceRef(asmDoc, c0.Item1, c0.Item2, asmFace, id);
                issues.Add(Issue("FACE_REF_REMATCHED", new[] { c0.Item1.Id }, new JObj {
                    { "face", id }, { "part", c0.Item1.Id }, { "score", Math.Round(Rematch.Score(stored, c0.Item3), 4) } }, new[] { id }));
            }
            return copy;
        }

        /// <summary>Surface type, exact area, centroid from a fine tessellation, normal (planes) or
        /// axis and radius (cylinders and cones), in the assembly frame, mm.</summary>
        public static JObj Fingerprint(IBody2 body, IFace2 face, Xform x)
        {
            var s = (ISurface)face.GetSurface();
            var o = new JObj { { "surface", SurfaceType(s) }, { "area", Math.Round(face.GetArea() * Units.M2_TO_MM2, 6) } };
            o["centroid"] = x.Point(TessCentroid(face)).ToRounded();
            if (s.IsPlane())
                o["normal"] = x.Direction(Vec3.From((double[])face.Normal).Unit()).ToRounded(9);
            else if (s.IsCylinder())
            {
                var cp = (double[])s.CylinderParams;
                o["axis"] = x.Direction(new Vec3(cp[3], cp[4], cp[5]).Unit()).ToRounded(9);
                o["radius"] = Math.Round(cp[6] * Units.M_TO_MM, 6);
            }
            else if (s.IsCone())
            {
                var cp = (double[])s.ConeParams;
                o["axis"] = x.Direction(new Vec3(cp[3], cp[4], cp[5]).Unit()).ToRounded(9);
            }
            return o;
        }

        public static string SurfaceType(ISurface s)
        {
            if (s.IsPlane()) return "plane";
            if (s.IsCylinder()) return "cylinder";
            if (s.IsCone()) return "cone";
            if (s.IsSphere()) return "sphere";
            if (s.IsTorus()) return "torus";
            if (s.Identity() == (int)swSurfaceTypes_e.BSURF_TYPE) return "bspline";
            return "other";
        }

        /// <summary>Area-weighted centroid of a fine tessellation of one face (part frame, mm).</summary>
        /// <summary>
        /// Area-weighted centroid of one face (part frame, mm) from the document's display
        /// tessellation, with each triangle's centre projected back onto the true surface, which
        /// removes most of the chord error on curved faces. (IBody2.GetTessellation, which
        /// takes a chord tolerance, throws a server fault in SolidWorks 2026: LIVE-T6.)
        /// </summary>
        private static Vec3 TessCentroid(IFace2 face)
        {
            var tri = face.GetTessTriangles(true) as float[];
            if (tri == null || tri.Length < 9) throw new ExtractException("a face has no tessellation");
            double area = 0;
            var sum = new Vec3(0, 0, 0);
            for (int i = 0; i + 8 < tri.Length; i += 9)
            {
                var v0 = new Vec3(tri[i], tri[i + 1], tri[i + 2]);
                var v1 = new Vec3(tri[i + 3], tri[i + 4], tri[i + 5]);
                var v2 = new Vec3(tri[i + 6], tri[i + 7], tri[i + 8]);
                double a = 0.5 * (v1 - v0).Cross(v2 - v0).Length;
                if (a <= 0) continue;
                var c = (v0 + v1 + v2) * (1.0 / 3.0);
                var on = face.GetClosestPointOn(c.X, c.Y, c.Z) as double[];
                if (on != null && on.Length >= 3) c = Vec3.From(on);
                sum = sum + c * a;
                area += a;
            }
            if (area <= 0) throw new ExtractException("empty tessellation for a face");
            return sum * (Units.M_TO_MM / area);
        }

        /// <summary>
        /// A face whose persistent reference still resolves may still have changed: SolidWorks
        /// keeps a split face's identity on one of the pieces (found live: a slot across a face
        /// left the load on one half, silently). When the area moved beyond the re-match
        /// tolerance: if coplanar (or coaxial) faces of the same body add up to the old area, the
        /// face was split and its loads must be re-selected (FACE_REF_LOST, blocking); otherwise
        /// it changed size, to confirm (FACE_REF_CHANGED, warning).
        /// </summary>
        private void CheckChanged(JObj stored, BodyEntry b, IFace2 partFace, JObj fresh, string id)
        {
            if (stored == null || !stored.Has("area")) return;
            double a0 = Convert.ToDouble(stored["area"]), a1 = Convert.ToDouble(fresh["area"]);
            if (a0 <= 1.0) return;                                   // a placeholder (set by automation), nothing to compare
            if (Math.Abs(a1 - a0) <= Rematch.AREA_RTOL * a0) return;
            var surf = (ISurface)partFace.GetSurface();
            var pieces = new List<double>();
            foreach (var f in ((object[])b.Body.GetFaces() ?? new object[0]).Cast<IFace2>())
                if (SameSurface(surf, partFace, (ISurface)f.GetSurface(), f)) pieces.Add(f.GetArea() * Units.M2_TO_MM2);
            double sum = pieces.Sum();
            if (pieces.Count > 1 && Math.Abs(sum - a0) <= Rematch.AREA_RTOL * a0)
            {
                issues.Add(Issue("FACE_REF_LOST", new[] { b.Id }, new JObj { { "face", id }, { "part", b.Id },
                    { "reason", string.Format(CultureInfo.InvariantCulture, "it was split into {0} faces ({1} mm2 in all; the reference now holds only {2:0.#} mm2 of it), so its loads and fixtures would act on part of it",
                                              pieces.Count, Math.Round(sum, 1), a1) } }, new[] { id }));
                return;
            }
            issues.Add(Issue("FACE_REF_CHANGED", new[] { b.Id }, new JObj { { "face", id }, { "part", b.Id },
                { "old_area", Math.Round(a0, 3) }, { "new_area", Math.Round(a1, 3) } }, new[] { id }));
        }

        /// <summary>Two faces on one underlying surface: coplanar planes, or coaxial cylinders of one radius.</summary>
        private static bool SameSurface(ISurface s0, IFace2 f0, ISurface s1, IFace2 f1)
        {
            if (s0.IsPlane() && s1.IsPlane())
            {
                var p0 = (double[])s0.PlaneParams; var p1 = (double[])s1.PlaneParams;    // normal xyz, root point xyz
                var n0 = new Vec3(p0[0], p0[1], p0[2]).Unit(); var n1 = new Vec3(p1[0], p1[1], p1[2]).Unit();
                if (Math.Abs(n0.Dot(n1)) < 0.999999) return false;
                return Math.Abs((new Vec3(p1[3], p1[4], p1[5]) - new Vec3(p0[3], p0[4], p0[5])).Dot(n0)) < 1e-6
                       && Vec3.From((double[])f0.Normal).Dot(Vec3.From((double[])f1.Normal)) > 0;     // same side, not the opposite skin
            }
            if (s0.IsCylinder() && s1.IsCylinder())
            {
                var c0 = (double[])s0.CylinderParams; var c1 = (double[])s1.CylinderParams;   // origin xyz, axis xyz, radius
                var a0 = new Vec3(c0[3], c0[4], c0[5]).Unit(); var a1 = new Vec3(c1[3], c1[4], c1[5]).Unit();
                if (Math.Abs(a0.Dot(a1)) < 0.999999 || Math.Abs(c0[6] - c1[6]) > 1e-9) return false;
                var d = new Vec3(c1[0], c1[1], c1[2]) - new Vec3(c0[0], c0[1], c0[2]);
                return (d - a0 * d.Dot(a0)).Length < 1e-6;
            }
            return false;
        }

        private static JObj Issue(string code, string[] parts, JObj prms, string[] faces = null)
        {
            var o = new JObj { { "code", code } };
            var where = new JObj();
            if (parts != null && parts.Length > 0) where["parts"] = parts.ToList();
            if (faces != null && faces.Length > 0) where["faces"] = faces.ToList();
            if (where.Any()) o["where"] = where;
            o["params"] = prms;
            return o;
        }
    }
}
