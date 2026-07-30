using System;
using System.IO;

namespace Anonymizer.Launcher
{
    // Native twin of Install.bat: explicit first-time setup (dependencies +
    // language models). You normally don't need it -- Anonymizer.exe runs the
    // same sync on first launch -- but it's here to set up deliberately.
    internal static class InstallLauncher
    {
        private static int Main()
        {
            try { Console.Title = "Anonymizer setup"; }
            catch (Exception) { /* no console title in some hosts; harmless */ }

            string uv = Shared.FindUv();
            if (uv == null)
            {
                Shared.UvMissing();
                Shared.Pause();
                return 1;
            }

            string setupScript = Path.Combine(Shared.AppDir, @"scripts\setup.ps1");
            if (!File.Exists(setupScript))
            {
                Shared.Say(@"Cannot find scripts\setup.ps1 next to this program.");
                Console.WriteLine("Keep Install.exe in the repository root.");
                Shared.Pause();
                return 1;
            }

            int exit = Shared.Run("powershell.exe",
                "-NoProfile -ExecutionPolicy Bypass -File \"" + setupScript + "\"");

            Console.WriteLine();
            if (exit == 0) Console.WriteLine("Done. Launch the app with Anonymizer.exe");
            else Shared.Say("Setup failed - see the messages above.");
            Shared.Pause();
            return exit;
        }
    }
}
