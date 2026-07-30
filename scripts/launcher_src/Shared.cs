using System;
using System.Diagnostics;
using System.IO;

namespace Anonymizer.Launcher
{
    // Shared plumbing for the two double-clickable launchers. Compiled into
    // both Anonymizer.exe and Install.exe -- see scripts/build_launchers.ps1.
    internal static class Shared
    {
        // Directory the running .exe lives in, i.e. the repository root.
        // Everything runs from here so a double-click behaves like the .bat
        // files' `cd /d "%~dp0"` regardless of the shell's current directory.
        internal static string AppDir
        {
            get
            {
                return Path.GetDirectoryName(
                    Process.GetCurrentProcess().MainModule.FileName);
            }
        }

        internal static void Say(string message)
        {
            Console.WriteLine("[Anonymizer] " + message);
        }

        // Locate uv.exe. PATH first (what the .bat did via `where uv`), then the
        // default install locations: a shell opened before uv was installed
        // hands down a stale PATH, and "uv is not installed" is a confusing
        // thing to tell someone who just installed it.
        internal static string FindUv()
        {
            string onPath = SearchPath("uv.exe");
            if (onPath != null) return onPath;

            string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string[] candidates = new string[]
            {
                Path.Combine(home, @".local\bin\uv.exe"),
                Path.Combine(home, @".cargo\bin\uv.exe"),
                Path.Combine(localAppData, @"Programs\uv\uv.exe"),
                Path.Combine(localAppData, @"uv\bin\uv.exe"),
            };
            foreach (string candidate in candidates)
            {
                if (File.Exists(candidate)) return candidate;
            }
            return null;
        }

        private static string SearchPath(string exeName)
        {
            string path = Environment.GetEnvironmentVariable("PATH");
            if (string.IsNullOrEmpty(path)) return null;

            foreach (string entry in path.Split(';'))
            {
                string dir = entry.Trim().Trim('"');
                if (dir.Length == 0) continue;
                string full;
                // A single malformed PATH entry must not sink the whole search.
                try { full = Path.Combine(dir, exeName); }
                catch (ArgumentException) { continue; }
                try { if (File.Exists(full)) return full; }
                catch (Exception) { continue; }
            }
            return null;
        }

        // Run a child process attached to this console so its output streams
        // live, exactly as it did when the .bat called the same command.
        internal static int Run(string exe, string arguments)
        {
            ProcessStartInfo psi = new ProcessStartInfo(exe, arguments);
            psi.UseShellExecute = false;
            psi.WorkingDirectory = AppDir;
            using (Process child = Process.Start(psi))
            {
                child.WaitForExit();
                return child.ExitCode;
            }
        }

        // A double-clicked console window closes on exit and takes the error
        // message with it. Hold it open, as the .bat files' `pause` did.
        internal static void Pause()
        {
            // With stdin redirected there is nobody to press a key, and
            // Console.ReadKey does NOT throw in that case -- it blocks forever,
            // so a launcher run from a script or scheduler would hang. cmd's
            // `pause` reads EOF and returns immediately; match that.
            if (Console.IsInputRedirected) return;

            Console.WriteLine();
            Console.Write("Press any key to close . . . ");
            try { Console.ReadKey(true); }
            catch (InvalidOperationException) { /* no console input available */ }
            Console.WriteLine();
        }

        internal static void UvMissing()
        {
            Say("'uv' is not installed.");
            Console.WriteLine("Install it from https://astral.sh/uv and run this again.");
        }
    }
}
