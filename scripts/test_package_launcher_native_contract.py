#!/usr/bin/env python3
"""Source/serialization contracts for the native app launcher (no app launch)."""
import hashlib
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "scripts/package_launcher.mm"
UI = ROOT / "scripts/package_launcher_ui.inc"
HELPER = ROOT / "scripts/replay_bundle.mm"


@unittest.skipUnless(sys.platform == "darwin", "AppKit/Foundation contracts require macOS")
class NativeLauncherContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = MAIN.read_text()
        cls.ui = UI.read_text()
        cls.helper = HELPER.read_text()

    def test_native_entrypoint_uses_persistent_control_window(self):
        self.assertIn("int main() { return runLauncherApp(); }", self.main)
        self.assertNotIn("execv(", self.main)
        self.assertIn("NSTask* task", self.ui)
        self.assertIn("task.terminationHandler", self.ui)
        self.assertIn("applicationShouldTerminateAfterLastWindowClosed", self.ui)

    def test_test_paths_fail_closed_and_data_operations_are_explicit(self):
        self.assertIn("if ((override && !LAbsoluteOverride(override))", self.ui)
        self.assertIn("exchangeOverride && !LAbsoluteOverride(exchangeOverride)", self.ui)
        self.assertIn("FUTARI_DIAGNOSTIC_AUTOSTART", self.ui)
        self.assertIn("FUTARI_DIAGNOSTIC_CHILD_STARTED pid=%d", self.ui)
        self.assertIn("FUTARI_DIAGNOSTIC_CHILD_EXIT pid=%d", self.ui)
        self.assertIn("LInstallContent(bundle, self.profileRoot, profile)", self.ui)
        self.assertLess(self.ui.index("flock(lockFD, LOCK_EX | LOCK_NB)"),
                        self.ui.index("LInstallContent(bundle, self.profileRoot, profile)"))
        self.assertIn("fstat(lockFD, &lockStat) != 0 || !S_ISREG(lockStat.st_mode)", self.ui)
        self.assertIn("FUTARI_PACKAGE_REPLAY_EXCHANGE", self.ui)
        self.assertIn("Desktop/360 Recomp/Replays", self.ui)
        self.assertIn('stringByAppendingPathComponent:@"Imports"', self.ui)
        self.assertIn('stringByAppendingPathComponent:@"Exports"', self.ui)
        self.assertIn("panel.directoryURL = [NSURL fileURLWithPath:imports isDirectory:YES]", self.ui)
        self.assertIn("panel.directoryURL = [NSURL fileURLWithPath:exports isDirectory:YES]", self.ui)
        self.assertIn('std::string finalPath = destination + "/" + archiveName;', self.helper)
        self.assertIn("EXPORT_DESTINATION", self.helper)
        self.assertIn("kArchiveLimit = 128ull * 1024 * 1024", self.helper)
        self.assertIn("launcher-settings.json", self.ui)
        self.assertIn("nothing is imported or exported automatically", self.ui)
        self.assertIn('keyEquivalent:@"q"', self.ui)
        self.assertIn("applicationShouldTerminateAfterLastWindowClosed", self.ui)

    def test_native_transfer_argument_order_and_child_exit_gate(self):
        self.assertIn('[args addObjectsFromArray:@[self.profileRoot, arguments[1], profile.content, profile.build]];', self.ui)
        self.assertIn('[args addObjectsFromArray:@[self.profileRoot, profile.content, profile.build]];', self.ui)
        self.assertIn("finished.processIdentifier", self.ui)
        self.assertIn('LJSONBool(receipt[@"clean_exit"], NO)', self.ui)
        self.assertIn("self.child.isRunning || self.helperTask.isRunning", self.ui)
        self.assertIn("Guest-thread drainage is not certified", self.ui)

    def test_pending_replay_recovery_runs_async_and_requires_another_play(self):
        launch = self.ui.split("- (void)launchGame:", 1)[1].split("- (void)runHelper:", 1)[0]
        helper = self.ui.split("- (void)runHelper:", 1)[1].split("- (void)importReplay:", 1)[0]
        self.assertIn('stringByAppendingPathComponent:@".futari-replay-import.pending"', self.ui)
        self.assertLess(launch.index("flock(lockFD, LOCK_EX | LOCK_NB)"),
                        launch.index("LPendingReplayImportPresent(self.profileRoot)"))
        self.assertLess(launch.index("LPendingReplayImportPresent(self.profileRoot)"),
                        launch.index("LInstallContent(bundle, self.profileRoot, profile)"))
        self.assertLess(launch.index("flock(self.lockFD, LOCK_UN)"),
                        launch.index('[self runHelper:@[@"--recover"]'))
        self.assertLess(launch.index("self.status.stringValue = @\"An interrupted replay import was found."),
                        launch.index('[self runHelper:@[@"--recover"]'))
        self.assertIn("O_NOFOLLOW | O_CLOEXEC", launch)
        self.assertIn('[arguments.firstObject isEqual:@"--recover"]', helper)
        self.assertIn('[self.profileRoot, profile.content, profile.build]', helper)
        self.assertIn("launchAndReturnError", helper)
        self.assertIn("Replay import recovery completed. The game was not launched; press Play when ready.", helper)
        self.assertIn("Replay import recovery stopped safely", helper)
        launch_failure = helper.split("} catch (const std::exception& e) {", 1)[1]
        self.assertIn("LCleanupUnlaunchedTask(helperTask, helperOutput)", launch_failure)
        self.assertIn("if (self.helperTask == helperTask) self.helperTask = nil", launch_failure)
        self.assertIn("[self updateControls]", launch_failure)

    def test_native_fd_and_settings_failure_cleanup(self):
        source = r'''#define FUTARI_LAUNCHER_TESTING 1
#import <AppKit/AppKit.h>
#include "package_launcher_ui.inc"
#include <string>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>

static int require(bool ok, const char* why) {
  if (!ok) { fprintf(stderr, "FAIL: %s\n", why); return 1; }
  return 0;
}
static NSData* data(const char* value) {
  return [NSData dataWithBytes:value length:strlen(value)];
}
int main(int argc, char** argv) { @autoreleasepool {
  if (argc != 2) return 2;
  NSString* root = [NSString stringWithUTF8String:argv[1]];
  NSString* settings = [root stringByAppendingPathComponent:@"launcher-settings.json"];
  NSData* original = data("old-settings");
  NSData* replacement = data("new-settings");
  if (require([original writeToFile:settings atomically:YES], "seed settings")) return 1;
  for (int point : {1, 2}) {
    bool failed = false;
    try { LWriteSettingsAtomicallyForTest(settings, replacement, point); }
    catch (const std::exception&) { failed = true; }
    if (require(failed, "injected settings write must fail")) return 1;
    NSData* actual = [NSData dataWithContentsOfFile:settings];
    if (require([actual isEqualToData:original], "failed settings update preserves old value")) return 1;
    NSArray<NSString*>* names = [[NSFileManager defaultManager] contentsOfDirectoryAtPath:root error:nil];
    for (NSString* name in names)
      if (require(![name hasPrefix:@".settings-"], "failed settings update removes unique temp")) return 1;
  }

  NSString* log = [root stringByAppendingPathComponent:@"session.log"];
  if (require([data("launcher marker\n") writeToFile:log atomically:YES], "seed append log")) return 1;
  for (int i = 0; i < 32; ++i) {
    @autoreleasepool {
      NSFileHandle* output = LOpenAppendLog(log);
      if (require(output != nil, "open append log")) return 1;
      NSTask* task = [[NSTask alloc] init];
      task.executableURL = [NSURL fileURLWithPath:[root stringByAppendingPathComponent:@"missing-executable"]];
      task.standardOutput = output; task.standardError = output;
      task.terminationHandler = ^(NSTask*) {};
      NSError* error = nil;
      if (require(![task launchAndReturnError:&error], "missing executable must fail to launch")) return 1;
      int fd = output.fileDescriptor;
      LCleanupUnlaunchedTask(task, output);
      if (require(task.terminationHandler == nil && task.standardOutput == nil && task.standardError == nil,
                  "failed task references are cleared")) return 1;
      errno = 0;
      if (require(fcntl(fd, F_GETFD) == -1 && errno == EBADF, "failed spawn closes owned log descriptor")) return 1;
    }
  }
  NSFileHandle* final = LOpenAppendLog(log);
  if (require(final != nil, "reopen append log")) return 1;
  [final writeData:data("last marker\n")]; [final closeFile];
  NSString* contents = [NSString stringWithContentsOfFile:log encoding:NSUTF8StringEncoding error:nil];
  if (require([contents hasPrefix:@"launcher marker\n"] && [contents hasSuffix:@"last marker\n"],
              "append writes preserve existing launcher marker")) return 1;
  return 0;
} }
'''
        with tempfile.TemporaryDirectory(prefix="futari-launcher-fd-contract-") as td:
            binary = Path(td) / "native-io-contract"
            fixture = Path(td) / "fixture"
            fixture.mkdir()
            built = subprocess.run(["/usr/bin/clang++", "-x", "objective-c++", "-std=c++17", "-fobjc-arc",
                                    "-framework", "AppKit", "-framework", "Foundation",
                                    "-I", str(ROOT / "scripts"), "-o", str(binary), "-"],
                                   input=source, text=True, capture_output=True, timeout=90)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            result = subprocess.run([str(binary), str(fixture)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_launcher_identity_avoids_signature_self_hash_cycle(self):
        validation = self.ui.split("static void LValidatePackageAndProfile", 1)[1].split("@interface", 1)[0]
        self.assertIn('identity_scheme"] isEqual:@"source_inventory_external_post_sign_receipt_v1', validation)
        self.assertIn("LUUID(package[@\"launcher\"][@\"packaged_uuid\"])", validation)
        self.assertIn('LJSONInteger(package[@"schema_version"], 3)', validation)
        self.assertNotIn('LHashRegularFile(launcherBinary', validation)
        self.assertIn('LHex(package[@"replay_helper"][@"packaged_sha256"])', validation)

    def test_foundation_compact_json_matches_python_build_identity_bytes(self):
        obj = {
            "schema_version": 1,
            "title_id": "435A07D7",
            "package_type": 2,
            "content": "both",
            "inputs": [{"input": "game/default.xex", "source_sha256": "a" * 64}],
            "launcher_sources": [{"path": "scripts/package_launcher_ui.inc", "source_sha256": "b" * 64}],
            "replay_helper_sources": {"source_sha256": "c" * 64, "header_sha256": "d" * 64},
            "files": [{"path": "00000000/435A07D7/caf\u00e9.bin", "bytes": 3, "sha256": "e" * 64}],
        }
        expected = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        source = r'''#import <Foundation/Foundation.h>
#include <cstdio>
int main() { @autoreleasepool {
  NSDictionary* o = @{
    @"schema_version": @1, @"title_id": @"435A07D7", @"package_type": @2, @"content": @"both",
    @"inputs": @[@{@"input": @"game/default.xex", @"source_sha256": [@"a" stringByPaddingToLength:64 withString:@"a" startingAtIndex:0]}],
    @"launcher_sources": @[@{@"path": @"scripts/package_launcher_ui.inc", @"source_sha256": [@"b" stringByPaddingToLength:64 withString:@"b" startingAtIndex:0]}],
    @"replay_helper_sources": @{@"source_sha256": [@"c" stringByPaddingToLength:64 withString:@"c" startingAtIndex:0], @"header_sha256": [@"d" stringByPaddingToLength:64 withString:@"d" startingAtIndex:0]},
    @"files": @[@{@"path": @"00000000/435A07D7/café.bin", @"bytes": @3, @"sha256": [@"e" stringByPaddingToLength:64 withString:@"e" startingAtIndex:0]}]
  };
  NSData* d = [NSJSONSerialization dataWithJSONObject:o options:NSJSONWritingSortedKeys | NSJSONWritingWithoutEscapingSlashes error:nil];
  fwrite(d.bytes, 1, d.length, stdout);
} }
'''
        with tempfile.TemporaryDirectory(prefix="futari-launcher-json-contract-") as td:
            binary = Path(td) / "serialize"
            built = subprocess.run(["/usr/bin/clang++", "-x", "objective-c++", "-std=c++17", "-fobjc-arc",
                                    "-framework", "Foundation", "-o", str(binary), "-"],
                                   input=source, text=True, capture_output=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            actual = subprocess.check_output([str(binary)], timeout=10)
        self.assertEqual(actual, expected)
        self.assertEqual(hashlib.sha256(actual).hexdigest(), hashlib.sha256(expected).hexdigest())


if __name__ == "__main__":
    unittest.main(verbosity=2)
