// Weld beads (cosmetic weld beads: Insert > Assembly Feature > Weld Bead) and sub-assembly
// membership. A bead is exported as its path in the assembly frame (polylines, mm), its
// size and options, and the parts it joins; the resolver bonds the touching faces within the
// bead size of the path (DECISIONS D22). Beads in sub-assemblies are read too: a
// sub-assembly is treated as rigid, like one multi-body part.
//
// API facts (SolidWorks 2026, LIVE-W1): beads sit in Weld Folder > Weld Sub Folder as
// "CosmeticWeldBead" features; ICosmeticWeldBeadFeatureData.GetEntitiesWeldPath gives the
// path edges (weld-path mode) and their curves are in the PART frame (an M8 edge read y = 0
// where the plate's underside is at y = 15 in the assembly); GetEntitiesWeldFrom/To (weld-
// geometry mode) take a ByRef entity type and return the two face sets.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;

namespace AsmFea.Extract
{
    public partial class Extractor
    {
        /// <summary>Points closer than this to a face are on it (weld-geometry mode path search), mm.</summary>
        private const double ON_FACE_TOL = 0.05;

        /// <summary>The top-level sub-assembly a component sits in ("Frame-1" for "Frame-1/Plate-2"), else null.</summary>
        public static string Subassembly(IComponent2 comp)
        {
            string n = comp.Name2 ?? "";
            int i = n.IndexOf('/');
            return i > 0 ? n.Substring(0, i) : null;
        }

        private List<JObj> Welds(IModelDoc2 rootDoc)
        {
            var welds = new List<JObj>();
            CollectWelds(rootDoc, "", welds);
            // beads inside sub-assemblies: their entities belong to the sub-assembly's components,
            // which are the root's components with the sub-assembly's name in front
            var asm = (IAssemblyDoc)rootDoc;
            foreach (var comp in ((object[])asm.GetComponents(false) ?? new object[0]).Cast<IComponent2>())
            {
                if (comp.IsSuppressed()) continue;
                var sub = comp.GetModelDoc2() as IModelDoc2;
                if (sub == null || sub.GetType() != (int)swDocumentTypes_e.swDocASSEMBLY) continue;
                CollectWelds(sub, comp.Name2 + "/", welds);
            }
            return welds;
        }

        private void CollectWelds(IModelDoc2 doc, string prefix, List<JObj> welds)
        {
            for (var f = doc.FirstFeature() as IFeature; f != null; f = f.GetNextFeature() as IFeature)
                WalkWeldFeature(doc, f, prefix, welds);
        }

        private void WalkWeldFeature(IModelDoc2 doc, IFeature f, string prefix, List<JObj> welds)
        {
            string type = f.GetTypeName2();
            if (type == "CosmeticWeldBead")
            {
                stage = "weld bead " + prefix + f.Name;
                try
                {
                    var w = Weld(doc, f, prefix);
                    if (w != null) welds.Add(w);
                }
                catch (Exception e)
                {
                    issues.Add(Issue("EXTRACT_ITEM_FAILED", null, new JObj {
                        { "item", "weld bead " + prefix + f.Name }, { "step", stage }, { "error", e.Message } }));
                    log.Add("FAILED at " + stage + ": " + e.Message);
                }
                return;
            }
            if (type.IndexOf("Weld", StringComparison.OrdinalIgnoreCase) < 0) return;   // only weld folders hold beads
            for (var sub = f.GetFirstSubFeature() as IFeature; sub != null; sub = sub.GetNextSubFeature() as IFeature)
                WalkWeldFeature(doc, sub, prefix, welds);
        }

        private JObj Weld(IModelDoc2 doc, IFeature f, string prefix)
        {
            var supp = f.IsSuppressed2((int)swInConfigurationOpts_e.swThisConfiguration, null) as Array;
            if (supp != null && supp.Length > 0 && Convert.ToBoolean(supp.GetValue(0))) return null;
            var d = f.GetDefinition() as ICosmeticWeldBeadFeatureData;
            if (d == null) throw new ExtractException("no weld bead definition");

            var parts = new List<string>();
            var paths = new List<object>();
            // weld-path mode: the path edges themselves
            foreach (var edge in ((object[])d.GetEntitiesWeldPath() ?? new object[0]).OfType<IEdge>())
            {
                var b = BodyOfEntity((IEntity)edge, prefix, ((IEdge)edge).GetBody() as IBody2);
                if (b == null) continue;
                if (!parts.Contains(b.Id)) parts.Add(b.Id);
                paths.Add(EdgePoints(edge).Select(p => (object)b.X.Point(p).ToRounded()).ToList());
            }
            // weld-geometry mode: the weld runs where the "from" faces meet the "to" faces
            int t;
            var fromFaces = ((object[])d.GetEntitiesWeldFrom(out t) ?? new object[0]).OfType<IFace2>().ToList();
            var toFaces = ((object[])d.GetEntitiesWeldTo(out t) ?? new object[0]).OfType<IFace2>().ToList();
            if (fromFaces.Count > 0 && toFaces.Count > 0)
            {
                var fromB = fromFaces.Select(x => Tuple.Create(x, BodyOfEntity((IEntity)x, prefix, x.GetBody() as IBody2))).Where(x => x.Item2 != null).ToList();
                var toB = toFaces.Select(x => Tuple.Create(x, BodyOfEntity((IEntity)x, prefix, x.GetBody() as IBody2))).Where(x => x.Item2 != null).ToList();
                foreach (var b in fromB.Concat(toB)) if (!parts.Contains(b.Item2.Id)) parts.Add(b.Item2.Id);
                foreach (var fa in fromB)
                    foreach (var edge in ((object[])fa.Item1.GetEdges() ?? new object[0]).OfType<IEdge>())
                    {
                        var pts = EdgePoints(edge).Select(p => fa.Item2.X.Point(p)).ToList();
                        if (toB.Any(fb => pts.All(p => OnFace(fb.Item1, fb.Item2, p))))
                            paths.Add(pts.Select(p => (object)p.ToRounded()).ToList());
                    }
            }
            if (parts.Count == 0) throw new ExtractException("its edges or faces could not be traced to an exported part");

            string side = d.Side == (int)swCosmeticWeldBeadSide_e.swCosmeticWeldBeadSide_bothSides ? "both_sides"
                        : d.Side == (int)swCosmeticWeldBeadSide_e.swCosmeticWeldBeadSide_allAround ? "all_around" : "selection";
            var o = new JObj
            {
                { "id", prefix + f.Name },
                { "size", Math.Round(d.BeadSize * Units.M_TO_MM, 6) },
                { "side", side },
                { "parts", parts },
                { "path", paths },
            };
            if (d.IntermittentWeld)
                o["intermittent"] = new JObj {
                    { "length", Math.Round(d.IntermittentWeldLength * Units.M_TO_MM, 6) },
                    { "gap", Math.Round(d.Gap * Units.M_TO_MM, 6) },
                    { "pitch", Math.Round(d.Pitch * Units.M_TO_MM, 6) },
                    { "staggered", d.Staggered } };
            if (d.FromToLength)
                o["partial"] = new JObj {
                    { "start", d.FromToStartPoint }, { "length", Math.Round(d.FromToWeldLength * Units.M_TO_MM, 6) },
                    { "reverse", d.FromToReverse } };
            return o;
        }

        /// <summary>The exported body an edge or face belongs to (by component name, then body name).</summary>
        private BodyEntry BodyOfEntity(IEntity ent, string prefix, IBody2 body)
        {
            var comp = ent.GetComponent() as IComponent2;
            if (comp == null) return null;
            string name = prefix + comp.Name2;
            var cands = bodies.Where(b => b.CompName == name).ToList();
            if (cands.Count <= 1 || body == null) return cands.FirstOrDefault();
            return cands.FirstOrDefault(b => b.Body.Name == body.Name) ?? cands[0];
        }

        /// <summary>Points along an edge in its part frame, mm (2 for a line, 17 otherwise).</summary>
        private static List<Vec3> EdgePoints(IEdge edge)
        {
            var cp = edge.GetCurveParams3() as CurveParamData;
            var c = edge.GetCurve() as ICurve;
            int n = c.IsLine() ? 2 : 17;
            var pts = new List<Vec3>();
            for (int i = 0; i < n; i++)
            {
                double u = cp.UMinValue + (cp.UMaxValue - cp.UMinValue) * i / (n - 1);
                var v = (double[])c.Evaluate2(u, 0);
                pts.Add(new Vec3(v[0], v[1], v[2]) * Units.M_TO_MM);
            }
            return pts;
        }

        /// <summary>Whether an assembly-frame point (mm) lies on a part face.</summary>
        private static bool OnFace(IFace2 face, BodyEntry b, Vec3 asmPoint)
        {
            var p = b.X.InversePoint(asmPoint) * (1.0 / Units.M_TO_MM);
            var r = face.GetClosestPointOn(p.X, p.Y, p.Z) as double[];
            if (r == null) return false;
            return (new Vec3(r[0], r[1], r[2]) - p).Length * Units.M_TO_MM < ON_FACE_TOL;
        }
    }
}
