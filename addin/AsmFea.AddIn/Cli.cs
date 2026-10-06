// The add-in drives the pipeline only through the asmfea CLI (PLAN.md "Interfaces"). Commands
// run on a worker thread so SolidWorks never blocks; results come back on the UI thread.
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;
using AsmFea.Extract;

namespace AsmFea.AddIn
{
    public static class Cli
    {
        /// <summary>
        /// The asmfea executable: ASMFEA_CLI, else "cli" under [addin] in
        /// %APPDATA%\asmfea\asmfea.toml (the pipeline's own config file), else the Python bundled by
        /// Setup.exe (run with Prefix()), else asmfea on PATH.
        /// </summary>
        public static string Exe()
        {
            string env = Environment.GetEnvironmentVariable("ASMFEA_CLI");
            if (!string.IsNullOrWhiteSpace(env)) return env.Trim();
            string toml = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "asmfea", "asmfea.toml");
            string fromToml = TomlValue(toml, "addin", "cli");
            if (!string.IsNullOrWhiteSpace(fromToml)) return fromToml;
            return BundledPython() ?? "asmfea";
        }

        /// <summary>Arguments in front of the command: "-m asmfea" when Exe() is the bundled Python.</summary>
        public static string Prefix()
        {
            return Exe() == BundledPython() ? "-m asmfea " : "";
        }

        /// <summary>An installed copy (Setup.exe): app\addin\AsmFea.AddIn.dll beside app\python\python.exe; else null.</summary>
        private static string BundledPython()
        {
            try
            {
                string here = Path.GetDirectoryName(typeof(Cli).Assembly.Location);
                string py = Path.GetFullPath(Path.Combine(here, "..", "python", "python.exe"));
                return File.Exists(py) ? py : null;
            }
            catch (Exception) { return null; }
        }

        /// <summary>One string value from a TOML file (section and key); enough for this config.</summary>
        public static string TomlValue(string path, string section, string key)
        {
            if (!File.Exists(path)) return null;
            string current = "";
            foreach (var raw in File.ReadAllLines(path))
            {
                string line = raw.Trim().TrimStart('﻿');
                if (line.StartsWith("#") || line.Length == 0) continue;
                if (line.StartsWith("[") && line.EndsWith("]")) { current = line.Substring(1, line.Length - 2).Trim(); continue; }
                int eq = line.IndexOf('=');
                if (current != section || eq < 0 || line.Substring(0, eq).Trim() != key) continue;
                string v = line.Substring(eq + 1).Trim();
                if (v.Length >= 2 && (v[0] == '\'' || v[0] == '"') && v[v.Length - 1] == v[0]) v = v.Substring(1, v.Length - 2);
                return v;
            }
            return null;
        }

        /// <summary>Run `asmfea --json args`; then call done(json or null, error text) on the control's thread.</summary>
        public static void RunAsync(Control owner, string args, Action<object, string> done)
        {
            Task.Run(() =>
            {
                object result = null;
                string error = null;
                try
                {
                    var psi = new ProcessStartInfo(Exe(), Prefix() + "--json " + args)
                    {
                        UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true,
                        RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8,
                    };
                    using (var p = Process.Start(psi))
                    {
                        var errTask = p.StandardError.ReadToEndAsync();
                        string output = p.StandardOutput.ReadToEnd();
                        p.WaitForExit();
                        string stderr = errTask.Result;
                        if (!string.IsNullOrWhiteSpace(output)) result = Json.Parse(output.Trim());
                        var jo = result as JObj;
                        if (jo != null && jo.Has("error")) error = jo["error"] + ": " + jo["message"];
                        else if (result == null) error = "asmfea gave no answer (exit " + p.ExitCode + "): " + Tail(stderr);
                    }
                }
                catch (Exception e)
                {
                    error = "could not run " + Exe() + ": " + e.Message;
                }
                if (owner.IsHandleCreated && !owner.IsDisposed)
                    owner.BeginInvoke(new Action(() =>
                    {
                        // a fault in a pane callback goes to the status line, never to an unhandled-exception
                        // dialog over SolidWorks
                        try { done(result, error); }
                        catch (Exception e)
                        {
                            var pane = owner as StudyPane;
                            if (pane != null) pane.ReportFault(e);
                        }
                    }));
            });
        }

        private static string Tail(string s)
        {
            s = (s ?? "").Trim();
            return s.Length > 400 ? "..." + s.Substring(s.Length - 400) : s;
        }

        public static string Quote(string path) { return "\"" + path + "\""; }
    }
}
