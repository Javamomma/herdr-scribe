// scribe-loopback.cs
//
// *** WINDOWS-ONLY. LIVE-ONLY. NOT BUILT OR RUN AS PART OF THIS REPO'S TEST
// SUITE. *** This source was authored on a Linux/WSL2 machine with no C#
// compiler or Windows audio stack available, so it has never been compiled
// or exercised against a real Windows host. Treat it as a careful first
// draft that needs a live Windows smoke test (build via
// scribe-loopback-setup.sh, then run it against a real call) before anyone
// relies on it.
//
// PURPOSE
//   herdr-scribe's optional "--teams" bridge: capture remote participants on
//   a video/voice call by looping back the machine's own audio *output*
//   (i.e. "what you hear") rather than trying to integrate with any specific
//   meeting app. Writes raw 16-bit PCM to stdout, forever, until the process
//   is terminated (e.g. by scribe.sh's capture-pipeline teardown trap) or a
//   write to stdout fails because the downstream reader went away.
//
// USERLAND ONLY, BY DESIGN
//   - No elevated/administrator privileges required to build or run this.
//   - No access to any meeting application's window, process, memory, or
//     account/tenant — this only ever touches the OS's public WASAPI
//     loopback capture API (via NAudio), which reads audio the OS is about
//     to send to the speakers. It cannot see anything a meeting app didn't
//     already choose to play out loud.
//   - No file is ever written by this program. Its only output is the
//     stdout stream, which scribe.sh's compose_capture() pipes straight
//     into scribe-transcribe.py — never to disk.
//
// BUILD
//   scribe-loopback-setup.sh compiles this with the in-box .NET Framework
//   C# compiler (csc.exe) plus a NAudio.dll placed alongside it — no Visual
//   Studio and no NuGet restore step required at build time on the target
//   machine.
//
// API NOTE
//   Written against NAudio's WasapiLoopbackCapture, which wraps the default
//   audio render (playback) device's WASAPI loopback stream — the standard,
//   documented mechanism for "what you hear" capture on Windows. Buffer
//   format/rate come from the device's mix format (typically 16/24/32-bit
//   float PCM at 44.1/48kHz). This program does NOT resample -- it writes
//   exactly what WASAPI hands it. scribe.sh's compose_capture() is what
//   resamples this stream to s16le/16k/mono (via ffmpeg) before it ever
//   reaches scribe-transcribe.py's STT backend, which hard-assumes that
//   format and does no resampling of its own.
using System;
using System.IO;
using System.Threading;
using NAudio.Wave;

internal static class ScribeLoopback
{
    private static int Main(string[] args)
    {
        try
        {
            using (var capture = new WasapiLoopbackCapture())
            using (var stdout = Console.OpenStandardOutput())
            {
                var stopped = new ManualResetEventSlim(false);
                Exception captureError = null;

                capture.DataAvailable += (sender, e) =>
                {
                    if (e.BytesRecorded <= 0)
                    {
                        return;
                    }
                    try
                    {
                        stdout.Write(e.Buffer, 0, e.BytesRecorded);
                        stdout.Flush();
                    }
                    catch (IOException)
                    {
                        // Downstream reader (the transcriber's stdin pipe) went
                        // away -- stop capturing rather than spin on write
                        // errors against a closed pipe.
                        stopped.Set();
                    }
                };

                capture.RecordingStopped += (sender, e) =>
                {
                    captureError = e.Exception;
                    stopped.Set();
                };

                capture.StartRecording();
                stopped.Wait();

                try
                {
                    capture.StopRecording();
                }
                catch (Exception)
                {
                    // Already stopping/stopped -- nothing more to do.
                }

                if (captureError != null)
                {
                    Console.Error.WriteLine(
                        "scribe-loopback: capture stopped with error: " + captureError.Message);
                    return 1;
                }

                return 0;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("scribe-loopback: fatal: " + ex.Message);
            return 1;
        }
    }
}
