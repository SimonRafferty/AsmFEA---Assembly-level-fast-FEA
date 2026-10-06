// Study data stored inside the assembly document (PLAN.md "Persistence and reference
// fallback"): one third-party storage stream per assembly, read when the document loads and
// written when the user saves it. Nothing reaches disk until the user saves.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
using AsmFea.Extract;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;

namespace AsmFea.AddIn
{
    /// <summary>The studies of every open assembly, keyed by the document.</summary>
    public class StudyRegistry
    {
        public const string STREAM = "AsmFEA_Studies";

        private class Entry
        {
            public IModelDoc2 Doc;
            public object Events;            // the AssemblyDoc or PartDoc whose events are watched (kept referenced)
            public StudyStore Store;
            public bool Loaded;
            public bool ReadFailed;          // never write over a stream we could not read
            public string LoadedHow = "";
        }

        private readonly List<Entry> entries = new List<Entry>();
        public readonly List<string> Log = new List<string>();

        /// <summary>Watch an assembly's or part's load and save so its studies travel with the file.</summary>
        public void Attach(IModelDoc2 doc)
        {
            if (doc == null || Find(doc) != null) return;
            int type = doc.GetType();
            if (type != (int)swDocumentTypes_e.swDocASSEMBLY && type != (int)swDocumentTypes_e.swDocPART) return;
            var e = new Entry { Doc = doc, Events = doc };
            Func<int> load = () =>
            {
                // a repeated load event must not throw away edits not yet saved into the document
                if (e.Loaded && e.Store != null && e.Store.Changed)
                {
                    StudyPane.Trace("load event for " + e.Doc.GetTitle() + ": kept the study (it has unsaved changes)");
                    return 0;
                }
                StudyPane.Trace("load event for " + e.Doc.GetTitle() + (e.Loaded ? ": study read again" : ": study read"));
                Read(e, "load event");
                return 0;
            };
            if (type == (int)swDocumentTypes_e.swDocASSEMBLY)
            {
                var asm = (AssemblyDoc)doc;
                asm.LoadFromStorageNotify += () => load();
                asm.SaveToStorageNotify += () => { Write(e); return 0; };
                asm.DestroyNotify2 += (int t) => { entries.Remove(e); return 0; };
            }
            else
            {
                var part = (PartDoc)doc;
                part.LoadFromStorageNotify += () => load();
                part.SaveToStorageNotify += () => { Write(e); return 0; };
                part.DestroyNotify2 += (int t) => { entries.Remove(e); return 0; };
            }
            entries.Add(e);
        }

        private Entry Find(IModelDoc2 doc)
        {
            foreach (var e in entries)
                if (ReferenceEquals(e.Doc, doc) || (e.Doc != null && SameDoc(e.Doc, doc))) return e;
            return null;
        }

        private static bool SameDoc(IModelDoc2 a, IModelDoc2 b)
        {
            try
            {
                string pa = a.GetPathName(), pb = b.GetPathName();
                if (!string.IsNullOrEmpty(pa) || !string.IsNullOrEmpty(pb))
                    return string.Equals(pa, pb, StringComparison.OrdinalIgnoreCase);
                return a.GetTitle() == b.GetTitle();
            }
            catch { return false; }
        }

        /// <summary>The studies of this assembly. Read from the document the first time when the
        /// load event came before the add-in could watch it (LIVE-M8a).</summary>
        public StudyStore StoreFor(IModelDoc2 doc)
        {
            Attach(doc);
            var e = Find(doc);
            if (e == null) return null;
            if (!e.Loaded) Read(e, "on demand");
            return e.Store;
        }

        /// <summary>How the store was read: "load event", "on demand", or "" (not yet).</summary>
        public string LoadedHow(IModelDoc2 doc)
        {
            var e = Find(doc);
            return e == null ? "" : e.LoadedHow;
        }

        /// <summary>Flag the document modified, so the user is asked to save and the save writes the studies.</summary>
        public void MarkChanged(IModelDoc2 doc)
        {
            var e = Find(doc);
            if (e == null) return;
            e.Store.Changed = true;
            doc.SetSaveFlag();
        }

        private void Read(Entry e, string how)
        {
            try
            {
                string text = ReadStream(e.Doc);
                e.Store = StudyStore.FromJson(text);
                e.LoadedHow = how + (text == null ? " (no stream)" : "");
            }
            catch (Exception ex)
            {
                // an unreadable stream is never overwritten: the store stays empty and saves skip it
                e.Store = new StudyStore();
                e.ReadFailed = true;
                e.LoadedHow = how + " (failed: " + ex.Message + ")";
                Log.Add("study data of " + e.Doc.GetTitle() + " could not be read: " + ex.Message);
            }
            e.Loaded = true;
        }

        private void Write(Entry e)
        {
            if (!e.Loaded || e.Store == null) return;              // never read: nothing to write back
            if (e.ReadFailed)
            {
                Log.Add("study data of " + e.Doc.GetTitle() + " was not saved: the stored data could not be read, so it was left as it was");
                return;
            }
            try
            {
                WriteStream(e.Doc, e.Store.ToJson());
                e.Store.Changed = false;
            }
            catch (Exception ex)
            {
                Log.Add("study data of " + e.Doc.GetTitle() + " could not be saved: " + ex.Message);
            }
        }

        // ------------------------------------------------------------------ the stream

        public static string ReadStream(IModelDoc2 doc)
        {
            var st = doc.IGet3rdPartyStorage(STREAM, false) as IStream;
            if (st == null) return null;
            try
            {
                System.Runtime.InteropServices.ComTypes.STATSTG stat;
                st.Stat(out stat, 1);
                var buf = new byte[stat.cbSize];
                if (buf.Length > 0) st.Read(buf, buf.Length, IntPtr.Zero);
                return Encoding.UTF8.GetString(buf);
            }
            finally
            {
                doc.IRelease3rdPartyStorage(STREAM);
                Marshal.ReleaseComObject(st);
            }
        }

        public static void WriteStream(IModelDoc2 doc, string text)
        {
            var st = doc.IGet3rdPartyStorage(STREAM, true) as IStream;
            if (st == null) throw new InvalidOperationException("SolidWorks gave no storage stream");
            try
            {
                var buf = Encoding.UTF8.GetBytes(text);
                st.Write(buf, buf.Length, IntPtr.Zero);
            }
            finally
            {
                doc.IRelease3rdPartyStorage(STREAM);
                Marshal.ReleaseComObject(st);
            }
        }
    }
}
