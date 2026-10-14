#!/usr/bin/env python3
"""Run the native launcher manifest/profile validator against an exact app bundle.

This is package-integrity validation only; it does not launch the GUI or game.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''#define main futari_launcher_entrypoint_main
#include "package_launcher.mm"
#undef main
#include <cstdio>
#include <cstring>
int main(int argc, const char** argv) { @autoreleasepool {
  if (argc == 4 && std::strcmp(argv[1], "--all") == 0) {
    NSString* bundle = [NSString stringWithUTF8String:argv[2]];
    NSString* installRoot = [NSString stringWithUTF8String:argv[3]];
    try { LMakeDirectory(installRoot); }
    catch (const std::exception& e) { std::fprintf(stderr, "FAIL install root: %s\n", e.what()); return 2; }
    for (NSString* content in @[@"base", @"ver101", @"blacklabel", @"both"]) {
      try {
        LProfile profile = LLoadProfile(bundle, content);
        LValidatePackageAndProfile(bundle, profile);
        NSString* target = [installRoot stringByAppendingPathComponent:content];
        LInstallContent(bundle, target, profile);
        LCheckProfileFiles(bundle, target, profile, true);
        LInstallContent(bundle, target, profile);
        LCheckProfileFiles(bundle, target, profile, false);
        std::printf("PASS %s validated+fresh-install+existing-install %s\n",
                    content.UTF8String, profile.build.UTF8String);
      } catch (const std::exception& e) {
        std::fprintf(stderr, "FAIL %s: %s\n", content.UTF8String, e.what()); return 2;
      }
    }
    return 0;
  }
  if (argc == 4 && std::strcmp(argv[1], "--reject-schema2") == 0) {
    NSString* sourceBundle = [NSString stringWithUTF8String:argv[2]];
    NSString* malformedBundle = [NSString stringWithUTF8String:argv[3]];
    try {
      LProfile profile = LLoadProfile(sourceBundle, @"base");
      LValidatePackageAndProfile(malformedBundle, profile);
      std::fprintf(stderr, "FAIL schema-2 package manifest was accepted\n"); return 3;
    } catch (const std::exception& e) {
      if (std::string(e.what()) != "The signed package manifest is missing or unsupported.") {
        std::fprintf(stderr, "FAIL rejected for wrong reason: %s\n", e.what()); return 4;
      }
      std::puts("PASS schema-2 package manifest rejected as unsupported"); return 0;
    }
  }
  if (argc == 3 && std::strcmp(argv[1], "--settings") == 0) {
    NSString* path = [NSString stringWithUTF8String:argv[2]];
    NSData* bytes = [NSData dataWithContentsOfFile:path];
    id value = bytes ? [NSJSONSerialization JSONObjectWithData:bytes options:0 error:nil] : nil;
    if (![value isKindOfClass:NSDictionary.class] ||
        !LJSONInteger(value[@"schema_version"], 1) || !LJSONBool(value[@"fullscreen"], YES)) {
      std::fprintf(stderr, "FAIL isolated fullscreen setting is not a JSON true boolean\n"); return 5;
    }
    std::puts("PASS settings fullscreen JSON boolean true parsed natively"); return 0;
  }
  std::fprintf(stderr, "Usage: native-harness --all APP INSTALL_ROOT | --reject-schema2 APP MALFORMED_APP\n");
  return 64;
} }
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path, help="exact signed .app bundle to validate")
    parser.add_argument("--receipt", type=Path, help="write a unique JSON validation receipt")
    parser.add_argument("--settings", type=Path, help="also verify an isolated native settings JSON file")
    args = parser.parse_args()
    app = args.app.resolve(strict=True)
    if not app.is_dir() or not app.name.endswith(".app"):
        parser.error("app must be an existing .app directory")

    manifest_path = app / "Contents/Resources/package-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 3:
        raise SystemExit(f"expected package manifest schema 3; got {manifest.get('schema_version')!r}")

    with tempfile.TemporaryDirectory(prefix="futari-native-manifest-validation-", dir="/private/tmp") as td:
        temp = Path(td)
        source = temp / "native_validation.mm"
        binary = temp / "native_validation"
        source.write_text(HARNESS)
        command = ["/usr/bin/clang++", "-x", "objective-c++", "-std=c++17", "-fobjc-arc",
                   "-framework", "AppKit", "-I", str(ROOT / "scripts"), str(source), "-o", str(binary)]
        built = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if built.returncode:
            raise SystemExit(built.stdout + built.stderr)
        installs = temp / "profile-installs"
        valid = subprocess.run([str(binary), "--all", str(app), str(installs)],
                               capture_output=True, text=True, timeout=360)
        print(valid.stdout, end="")
        if valid.returncode:
            raise SystemExit(valid.stderr or f"native validation failed with status {valid.returncode}")

        malformed = temp / "SchemaTwo.app"
        resource = malformed / "Contents/Resources"
        resource.mkdir(parents=True)
        (resource / "package-manifest.json").write_text('{"schema_version":2}\n')
        rejected = subprocess.run([str(binary), "--reject-schema2", str(app), str(malformed)],
                                  capture_output=True, text=True, timeout=30)
        print(rejected.stdout, end="")
        if rejected.returncode:
            raise SystemExit(rejected.stderr or f"schema-2 rejection failed with status {rejected.returncode}")
        settings_result = None
        if args.settings:
            settings_result = subprocess.run([str(binary), "--settings", str(args.settings.resolve())],
                                             capture_output=True, text=True, timeout=30)
            print(settings_result.stdout, end="")
            if settings_result.returncode:
                raise SystemExit(settings_result.stderr or
                                 f"native settings check failed with status {settings_result.returncode}")
        if args.receipt:
            receipt = {
                "receipt_type": "native_launcher_package_validation_v1",
                "candidate_app": str(app),
                "package_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "launcher_validator_source_sha256": hashlib.sha256(
                    (ROOT / "scripts/package_launcher_ui.inc").read_bytes()).hexdigest(),
                "profiles": valid.stdout.splitlines(),
                "schema2_rejection": rejected.stdout.strip(),
                "settings_json_native_check": settings_result.stdout.strip() if settings_result else None,
                "gameplay_or_playback_tested": False,
                "invocation": [sys.executable, str(Path(__file__).resolve()), str(app),
                               "--receipt", str(args.receipt.resolve())],
            }
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            with args.receipt.open("x", encoding="utf-8") as f:
                json.dump(receipt, f, indent=2)
                f.write("\n")
            print(f"RECEIPT {args.receipt.resolve()}")


if __name__ == "__main__":
    main()
