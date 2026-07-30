using System;
using System.IO;

namespace Anonymizer.Launcher
{
    // Native twin of Anonymizer.bat. Syncs dependencies (fast when already up
    // to date, so a fresh `git pull` just works), then launches the app.
    internal static class AnonymizerLauncher
    {
        private static int Main()
        {
            try { Console.Title = "Anonymizer"; }
            catch (Exception) { /* no console title in some hosts; harmless */ }

            string uv = Shared.FindUv();
            if (uv == null)
            {
                Shared.UvMissing();
                Shared.Pause();
                return 1;
            }

            Shared.Say("Checking environment (first run / after an update downloads models, a few minutes)...");

            // `uv sync` installs EXACTLY the declared dependencies and PRUNES
            // anything else, including optional extras. A plain `uv sync` here
            // therefore uninstalled the ML detection stack on every launch -- so
            // AI detection, once switched on, would hard-fail the next time the
            // app was started this way. Sync the `ml` extra whenever a model
            // pack is actually present, and stay lean otherwise so a colleague
            // who never uses AI detection is not made to download ~700 MB of
            // torch.
            string extras = "";
            bool modelPackPresent =
                Directory.Exists(Path.Combine(Shared.AppDir, @"vendor\gliner-model"));
            bool modelPathConfigured =
                !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("ANONYMIZER_GLINER_MODEL"));
            if (modelPackPresent || modelPathConfigured) extras = " --extra ml";

            if (Shared.Run(uv, "sync" + extras) != 0)
            {
                Shared.Say("Environment setup failed - see the messages above.");
                Shared.Pause();
                return 1;
            }

            Shared.Say("Starting...");
            int exit = Shared.Run(uv, "run anonymizer");
            if (exit != 0)
            {
                Console.WriteLine();
                Shared.Say("Something went wrong starting the app.");
                Shared.Pause();
            }
            return exit;
        }
    }
}
