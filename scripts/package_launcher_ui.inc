// AppKit control surface for the personal Futari package. Included by
// package_launcher.mm so the packager builds one small native launcher.
#import <CommonCrypto/CommonDigest.h>
#import <CoreFoundation/CoreFoundation.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/stdio.h>
#include <fcntl.h>
#include <unistd.h>
#include <copyfile.h>
#include <algorithm>
#include <cerrno>
#include <set>
#include <stdexcept>

static NSString* LString(const std::string& s) {
  return [[NSString alloc] initWithBytes:s.data() length:s.size() encoding:NSUTF8StringEncoding];
}
static std::string LUtf8(NSString* s) {
  const char* p = s.UTF8String;
  if (!p) throw std::runtime_error("Invalid UTF-8 path or manifest text");
  return p;
}
static void LRequire(bool ok, NSString* message) {
  if (!ok) throw std::runtime_error(LUtf8(message));
}
static bool LHex(NSString* s) {
  if (![s isKindOfClass:NSString.class] || s.length != 64) return false;
  NSCharacterSet* nonHex = [[NSCharacterSet characterSetWithCharactersInString:
      @"0123456789abcdef"] invertedSet];
  return [s rangeOfCharacterFromSet:nonHex].location == NSNotFound;
}
static bool LUUID(NSString* s) {
  if (![s isKindOfClass:NSString.class] || s.length != 36) return false;
  for (NSUInteger i = 0; i < s.length; ++i) {
    unichar c = [s characterAtIndex:i];
    if (i == 8 || i == 13 || i == 18 || i == 23) {
      if (c != '-') return false;
    } else if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'F'))) {
      return false;
    }
  }
  return true;
}
static bool LAbsoluteOverride(const char* raw) {
  if (!raw || raw[0] != '/' || raw[1] == '\0') return false;
  NSString* path = [NSString stringWithUTF8String:raw];
  if (!path) return false;
  for (NSString* part in [path componentsSeparatedByString:@"/"])
    if ([part isEqualToString:@"."] || [part isEqualToString:@".."] ) return false;
  return true;
}
static NSString* LHash(NSData* data) {
  unsigned char digest[CC_SHA256_DIGEST_LENGTH];
  CC_SHA256(data.bytes, (CC_LONG)data.length, digest);
  static const char hex[] = "0123456789abcdef";
  char out[CC_SHA256_DIGEST_LENGTH * 2 + 1];
  for (size_t i = 0; i < CC_SHA256_DIGEST_LENGTH; ++i) {
    out[i * 2] = hex[digest[i] >> 4]; out[i * 2 + 1] = hex[digest[i] & 15];
  }
  out[sizeof(out) - 1] = 0;
  return [NSString stringWithUTF8String:out];
}
static bool LJSONBool(id value, BOOL wanted) {
  return [value isKindOfClass:NSNumber.class] &&
      CFGetTypeID((CFTypeRef)value) == CFBooleanGetTypeID() && [value boolValue] == wanted;
}
static bool LJSONInteger(id value, long long wanted) {
  if (![value isKindOfClass:NSNumber.class]) return false;
  NSNumber* number = value;
  const char* type = number.objCType;
  bool integer = strcmp(type, @encode(int)) == 0 || strcmp(type, @encode(long)) == 0 ||
      strcmp(type, @encode(long long)) == 0 || strcmp(type, @encode(unsigned int)) == 0 ||
      strcmp(type, @encode(unsigned long)) == 0 || strcmp(type, @encode(unsigned long long)) == 0;
  return integer && number.longLongValue == wanted;
}
static void LNoSymlinkPath(NSString* path) {
  LRequire(path.isAbsolutePath, @"Expected an absolute path.");
  NSArray<NSString*>* pieces = path.pathComponents;
  NSString* current = @"/";
  for (NSUInteger i = 1; i < pieces.count; ++i) {
    NSString* part = pieces[i];
    LRequire(![part isEqualToString:@"."] && ![part isEqualToString:@".."],
             @"Unsafe path component.");
    current = [current stringByAppendingPathComponent:part];
    struct stat st;
    if (lstat(current.fileSystemRepresentation, &st) == 0)
      LRequire(!S_ISLNK(st.st_mode), [@"Symlink path is not allowed: " stringByAppendingString:current]);
    else LRequire(errno == ENOENT, @"Could not inspect a path component.");
  }
}
static void LMakeDirectory(NSString* path) {
  LNoSymlinkPath(path);
  struct stat st;
  if (lstat(path.fileSystemRepresentation, &st) == 0) {
    LRequire(S_ISDIR(st.st_mode), [@"Expected a folder: " stringByAppendingString:path]);
    return;
  }
  LRequire(errno == ENOENT, @"Could not inspect a folder path.");
  NSString* parent = path.stringByDeletingLastPathComponent;
  if (![parent isEqualToString:@"/"]) LMakeDirectory(parent);
  LRequire(mkdir(path.fileSystemRepresentation, 0700) == 0 || errno == EEXIST,
           [@"Could not create folder: " stringByAppendingString:path]);
  LNoSymlinkPath(path);
}
static NSString* LSafeMember(NSString* rel) {
  LRequire([rel isKindOfClass:NSString.class] && rel.length > 0 && rel.length <= 240 &&
           !rel.isAbsolutePath && ![rel containsString:@"\\"] && ![rel containsString:@":"] &&
           ![rel containsString:@"\0"], @"Unsafe profile file path.");
  for (NSString* part in [rel componentsSeparatedByString:@"/"])
    LRequire(part.length > 0 && ![part isEqualToString:@"."] && ![part isEqualToString:@".."] &&
             ![part hasPrefix:@"."], @"Unsafe profile file path.");
  return rel;
}
static NSData* LReadRegular(NSString* path, NSUInteger limit) {
  LNoSymlinkPath(path);
  int fd = open(path.fileSystemRepresentation, O_RDONLY | O_NOFOLLOW);
  LRequire(fd >= 0, [@"Could not open file: " stringByAppendingString:path]);
  struct stat st;
  if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size < 0 ||
      (uint64_t)st.st_size > limit) { close(fd); throw std::runtime_error("Unexpected file type or size."); }
  NSMutableData* data = [NSMutableData dataWithLength:(NSUInteger)st.st_size];
  size_t offset = 0;
  while (offset < data.length) {
    ssize_t n = read(fd, (uint8_t*)data.mutableBytes + offset, data.length - offset);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) { close(fd); throw std::runtime_error("Short file read."); }
    offset += (size_t)n;
  }
  close(fd); return data;
}
static NSString* LHashRegularFile(NSString* path, uint64_t* byteCount) {
  LNoSymlinkPath(path);
  int fd = open(path.fileSystemRepresentation, O_RDONLY | O_NOFOLLOW);
  LRequire(fd >= 0, [@"Could not open packaged file: " stringByAppendingString:path]);
  struct stat st;
  if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size < 0) {
    close(fd); throw std::runtime_error("Unexpected packaged file type or size.");
  }
  CC_SHA256_CTX ctx; CC_SHA256_Init(&ctx);
  uint8_t chunk[1024 * 1024]; uint64_t total = 0;
  for (;;) {
    ssize_t n = read(fd, chunk, sizeof(chunk));
    if (n < 0 && errno == EINTR) continue;
    if (n < 0) { close(fd); throw std::runtime_error("Could not read packaged file."); }
    if (n == 0) break;
    total += (uint64_t)n; CC_SHA256_Update(&ctx, chunk, (CC_LONG)n);
  }
  close(fd);
  uint8_t digest[CC_SHA256_DIGEST_LENGTH]; CC_SHA256_Final(digest, &ctx);
  static const char hex[] = "0123456789abcdef"; char out[65];
  for (size_t i = 0; i < sizeof(digest); ++i) { out[i * 2] = hex[digest[i] >> 4]; out[i * 2 + 1] = hex[digest[i] & 15]; }
  out[64] = 0; if (byteCount) *byteCount = total;
  return [NSString stringWithUTF8String:out];
}
static bool LMatchesProfileRecord(NSString* path, NSDictionary* row) {
  uint64_t size = 0;
  NSString* digest = LHashRegularFile(path, &size);
  return size == [row[@"bytes"] unsignedLongLongValue] && [digest isEqual:row[@"sha256"]];
}
static void LCopyProfileRecordExclusive(NSString* source, NSString* target, NSDictionary* row) {
  LNoSymlinkPath(source);
  LNoSymlinkPath(target.stringByDeletingLastPathComponent);
  uint64_t expected = [row[@"bytes"] unsignedLongLongValue];
  int sourceFD = open(source.fileSystemRepresentation, O_RDONLY | O_NOFOLLOW);
  LRequire(sourceFD >= 0, [@"Could not open packaged content: " stringByAppendingString:source]);
  struct stat sourceStat;
  if (fstat(sourceFD, &sourceStat) != 0 || !S_ISREG(sourceStat.st_mode) || sourceStat.st_size < 0 ||
      (uint64_t)sourceStat.st_size != expected) {
    close(sourceFD);
    throw std::runtime_error("Packaged content source type or size changed before copy.");
  }
  int targetFD = open(target.fileSystemRepresentation, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
  if (targetFD < 0) { close(sourceFD); throw std::runtime_error("Could not create exclusive profile content file."); }
  bool targetCreated = true;
  try {
    uint8_t buffer[1024 * 1024];
    uint64_t total = 0;
    for (;;) {
      ssize_t count = read(sourceFD, buffer, sizeof(buffer));
      if (count < 0 && errno == EINTR) continue;
      LRequire(count >= 0, @"Could not read packaged profile content.");
      if (count == 0) break;
      total += static_cast<uint64_t>(count);
      LRequire(total <= expected, @"Packaged content grew while it was copied.");
      size_t offset = 0;
      while (offset < static_cast<size_t>(count)) {
        ssize_t written = write(targetFD, buffer + offset, static_cast<size_t>(count) - offset);
        if (written < 0 && errno == EINTR) continue;
        LRequire(written > 0, @"Could not write staged profile content.");
        offset += static_cast<size_t>(written);
      }
    }
    LRequire(total == expected, @"Packaged content changed size while it was copied.");
    LRequire(fsync(targetFD) == 0, @"Could not sync staged profile content.");
    close(targetFD); targetFD = -1;
    close(sourceFD); sourceFD = -1;
    LRequire(LMatchesProfileRecord(target, row), @"Copied content did not match its manifest.");
    targetCreated = false;
  } catch (...) {
    if (targetFD >= 0) close(targetFD);
    if (sourceFD >= 0) close(sourceFD);
    if (targetCreated) unlink(target.fileSystemRepresentation);
    throw;
  }
}
static NSString* LBundleMember(NSString* bundle, NSString* relative) {
  LSafeMember(relative);
  NSString* path = [bundle stringByAppendingPathComponent:relative];
  LNoSymlinkPath(path);
  return path;
}

struct LProfile {
  NSString* content;
  NSString* build;
  NSString* manifestPath;
  NSDictionary* manifest;
  NSArray<NSDictionary*>* files;
};

static LProfile LLoadProfile(NSString* bundle, NSString* content) {
  NSSet* allowed = [NSSet setWithArray:@[@"base", @"ver101", @"blacklabel", @"both"]];
  LRequire([allowed containsObject:content], @"Choose a supported content profile.");
  NSString* profileDir = [[bundle stringByAppendingPathComponent:@"Contents/Resources/ContentProfiles"]
      stringByAppendingPathComponent:content];
  NSString* manifestPath = [profileDir stringByAppendingPathComponent:@"content-profile-manifest.json"];
  NSData* bytes = LReadRegular(manifestPath, 1024 * 1024);
  NSError* error = nil;
  id json = [NSJSONSerialization JSONObjectWithData:bytes options:NSJSONReadingFragmentsAllowed error:&error];
  LRequire([json isKindOfClass:NSDictionary.class], @"The selected content manifest is invalid.");
  NSDictionary* m = json;
  LRequire(LJSONInteger(m[@"schema_version"], 1) &&
           [m[@"title_id"] isEqual:@"435A07D7"] && LJSONInteger(m[@"package_type"], 2) &&
           [m[@"content"] isEqual:content] && LHex(m[@"build_identity_sha256"]),
           @"The selected content manifest does not match this app or its build.");
  id entries = m[@"files"];
  LRequire([entries isKindOfClass:NSArray.class], @"The content manifest file roster is invalid.");
  for (id row in entries)
    LRequire([row isKindOfClass:NSDictionary.class] && [row[@"path"] isKindOfClass:NSString.class],
             @"The content manifest contains an invalid file record.");
  NSArray* sorted = [entries sortedArrayUsingComparator:^NSComparisonResult(NSDictionary* a, NSDictionary* b) {
    return [a[@"path"] compare:b[@"path"] options:NSLiteralSearch];
  }];
  NSMutableSet* seen = [NSMutableSet set];
  for (id row in sorted) {
    NSNumber* size = [row isKindOfClass:NSDictionary.class] ? row[@"bytes"] : nil;
    const char* numberType = size ? size.objCType : "";
    bool integerSize = size && (strcmp(numberType, @encode(int)) == 0 || strcmp(numberType, @encode(long)) == 0 ||
        strcmp(numberType, @encode(long long)) == 0 || strcmp(numberType, @encode(unsigned int)) == 0 ||
        strcmp(numberType, @encode(unsigned long)) == 0 || strcmp(numberType, @encode(unsigned long long)) == 0);
    LRequire([row isKindOfClass:NSDictionary.class] && [row[@"path"] isKindOfClass:NSString.class] &&
             integerSize && size.longLongValue >= 0 && size.longLongValue <= 512ll * 1024 * 1024 &&
             LHex(row[@"sha256"]),
             @"The content manifest contains an invalid file record.");
    NSString* rel = LSafeMember(row[@"path"]);
    NSString* folded = rel.precomposedStringWithCanonicalMapping.lowercaseString.decomposedStringWithCanonicalMapping;
    LRequire(![seen containsObject:folded], @"The content manifest repeats or case-collides a file path.");
    [seen addObject:folded];
  }
  LRequire([content isEqual:@"base"] ? sorted.count == 0 : sorted.count > 0,
           @"The selected content manifest has an unexpected empty file roster.");
  return {content, m[@"build_identity_sha256"], manifestPath, m, sorted};
}

// Recompute the packager's canonical identity from signed package inputs and
// the selected profile manifest. Object keys are sorted, JSON is compact UTF-8;
// arrays are sorted by input/path before serialization.
static NSString* LCanonicalBuildIdentity(NSDictionary* package, LProfile profile) {
  NSArray* launcherSources = package[@"launcher_sources"];
  NSDictionary* helper = package[@"replay_helper"];
  NSArray* inputs = package[@"inputs"];
  LRequire([launcherSources isKindOfClass:NSArray.class] && launcherSources.count == 2 &&
           [helper isKindOfClass:NSDictionary.class] && [inputs isKindOfClass:NSArray.class] && inputs.count == 15,
           @"The package manifest is missing required build-identity inputs.");
  LRequire(LHex(helper[@"source_sha256"]) && LHex(helper[@"header_sha256"]),
           @"The replay-helper identity is malformed.");
  for (id row in inputs)
    LRequire([row isKindOfClass:NSDictionary.class] && [row[@"input"] isKindOfClass:NSString.class] &&
             LHex(row[@"source_sha256"]), @"The package input identity is malformed.");
  for (id row in launcherSources)
    LRequire([row isKindOfClass:NSDictionary.class] && [row[@"path"] isKindOfClass:NSString.class] &&
             LHex(row[@"source_sha256"]), @"The launcher source inventory is malformed.");
  NSArray* sortedInputs = [inputs sortedArrayUsingComparator:^NSComparisonResult(NSDictionary* a, NSDictionary* b) {
    return [a[@"input"] compare:b[@"input"] options:NSLiteralSearch];
  }];
  NSMutableArray* inputRows = [NSMutableArray array];
  for (NSDictionary* row in sortedInputs) {
    LRequire([row isKindOfClass:NSDictionary.class] && [row[@"input"] isKindOfClass:NSString.class] &&
             LHex(row[@"source_sha256"]), @"The package input identity is malformed.");
    [inputRows addObject:@{@"input": row[@"input"], @"source_sha256": row[@"source_sha256"]}];
  }
  NSArray* sortedLauncherSources = [launcherSources sortedArrayUsingComparator:^NSComparisonResult(NSDictionary* a, NSDictionary* b) {
    return [a[@"path"] compare:b[@"path"] options:NSLiteralSearch];
  }];
  NSMutableArray* launcherRows = [NSMutableArray array];
  for (NSDictionary* row in sortedLauncherSources) {
    [launcherRows addObject:@{@"path": row[@"path"], @"source_sha256": row[@"source_sha256"]}];
  }
  NSDictionary* helperSources = package[@"helper_sources"];
  LRequire([helperSources isKindOfClass:NSDictionary.class] &&
           [helperSources[@"replay_bundle_source_sha256"] isEqual:helper[@"source_sha256"]] &&
           [helperSources[@"replay_bundle_header_sha256"] isEqual:helper[@"header_sha256"]],
           @"The native replay helper source inventory is inconsistent.");
  NSMutableArray* fileRows = [NSMutableArray array];
  for (NSDictionary* row in profile.files)
    [fileRows addObject:@{@"path": row[@"path"], @"bytes": row[@"bytes"], @"sha256": row[@"sha256"]}];
  NSDictionary* identity = @{
    @"schema_version": @1, @"title_id": @"435A07D7", @"package_type": @2,
    @"content": profile.content, @"inputs": inputRows,
    @"launcher_sources": launcherRows,
    @"replay_helper_sources": @{@"source_sha256": helper[@"source_sha256"], @"header_sha256": helper[@"header_sha256"]},
    @"files": fileRows
  };
  NSError* error = nil;
  NSData* canonical = [NSJSONSerialization dataWithJSONObject:identity
      options:NSJSONWritingSortedKeys | NSJSONWritingWithoutEscapingSlashes error:&error];
  LRequire(canonical != nil, @"Could not serialize the build identity.");
  return LHash(canonical);
}

static void LValidatePackageAndProfile(NSString* bundle, LProfile profile) {
  NSString* packagePath = [bundle stringByAppendingPathComponent:@"Contents/Resources/package-manifest.json"];
  NSData* bytes = LReadRegular(packagePath, 8 * 1024 * 1024);
  NSError* error = nil;
  NSDictionary* package = [NSJSONSerialization JSONObjectWithData:bytes options:0 error:&error];
  LRequire([package isKindOfClass:NSDictionary.class] && LJSONInteger(package[@"schema_version"], 3),
           @"The signed package manifest is missing or unsupported.");
  LRequire([package[@"replay_build_identities"] isKindOfClass:NSDictionary.class] &&
           [package[@"content_profiles"] isKindOfClass:NSDictionary.class] &&
           [package[@"launcher"] isKindOfClass:NSDictionary.class] &&
           [package[@"replay_helper"] isKindOfClass:NSDictionary.class] &&
           [package[@"launcher_sources"] isKindOfClass:NSArray.class] &&
           [package[@"inputs"] isKindOfClass:NSArray.class],
           @"The signed package inventory is incomplete or malformed.");
  NSString* expected = package[@"replay_build_identities"][profile.content];
  NSString* actual = LCanonicalBuildIdentity(package, profile);
  NSString* manifestHash = LHash(LReadRegular(profile.manifestPath, 1024 * 1024));
  NSString* packagedManifestHash = package[@"content_profiles"][profile.content][@"manifest_sha256"];
  LRequire(LHex(expected) && [expected isEqual:actual] && [profile.build isEqual:actual],
           @"The selected content or executable set does not match its signed build identity.");
  LRequire(LHex(packagedManifestHash) && [manifestHash isEqual:packagedManifestHash],
           @"The selected content manifest differs from the signed package inventory.");
  LRequire([package[@"launcher"][@"packaged_path"] isKindOfClass:NSString.class] &&
           [package[@"launcher"][@"identity_scheme"] isEqual:@"source_inventory_external_post_sign_receipt_v1"] &&
           LHex(package[@"launcher"][@"source_sha256"]) &&
           LUUID(package[@"launcher"][@"packaged_uuid"]) &&
           [package[@"replay_helper"][@"packaged_path"] isKindOfClass:NSString.class] &&
           LHex(package[@"replay_helper"][@"packaged_sha256"]),
           @"The launcher/helper package identities are malformed.");
  NSString* helperBinary = LBundleMember(bundle, package[@"replay_helper"][@"packaged_path"]);
  uint64_t helperSize = 0;
  LRequire([LHashRegularFile(helperBinary, &helperSize) isEqual:package[@"replay_helper"][@"packaged_sha256"]],
           @"The packaged replay helper differs from its signed inventory.");
  for (NSDictionary* row in package[@"inputs"]) {
    LRequire([row isKindOfClass:NSDictionary.class] && [row[@"packaged_path"] isKindOfClass:NSString.class] &&
             [row[@"input"] isKindOfClass:NSString.class] && LHex(row[@"packaged_sha256"]),
             @"The package file inventory is malformed.");
    NSString* path = LBundleMember(bundle, row[@"packaged_path"]);
    uint64_t size = 0;
    LRequire([LHashRegularFile(path, &size) isEqual:row[@"packaged_sha256"]],
             [@"A packaged executable or runtime file differs from its inventory: " stringByAppendingString:row[@"input"]]);
  }
  NSDictionary* gameData = package[@"game_data_sha256"];
  LRequire([gameData isKindOfClass:NSDictionary.class] && gameData.count > 0,
           @"The package game-data inventory is missing.");
  NSString* gameDataRoot = @"Contents/Resources/GameData";
  for (NSString* relative in gameData) {
    LSafeMember(relative);
    NSString* path = LBundleMember(bundle, [gameDataRoot stringByAppendingPathComponent:relative]);
    uint64_t size = 0;
    LRequire(LHex(gameData[relative]) && [LHashRegularFile(path, &size) isEqual:gameData[relative]],
             [@"Packaged game data differs from its inventory: " stringByAppendingString:relative]);
  }
  NSString* resourceRoot = [[bundle stringByAppendingPathComponent:@"Contents/Resources/ContentProfiles"]
      stringByAppendingPathComponent:profile.content];
  for (NSDictionary* row in profile.files) {
    NSString* rel = LSafeMember(row[@"path"]);
    NSString* source = [resourceRoot stringByAppendingPathComponent:rel];
    LRequire(LMatchesProfileRecord(source, row),
             [@"Packaged content file failed verification: " stringByAppendingString:rel]);
  }
}

static void LCheckProfileFiles(NSString* bundle, NSString* profileRoot, LProfile profile, bool fillMissing) {
  LNoSymlinkPath(profileRoot);
  NSString* resourceRoot = [[bundle stringByAppendingPathComponent:@"Contents/Resources/ContentProfiles"]
      stringByAppendingPathComponent:profile.content];
  for (NSDictionary* row in profile.files) {
    NSString* rel = LSafeMember(row[@"path"]);
    NSString* target = [profileRoot stringByAppendingPathComponent:rel];
    struct stat st;
    if (lstat(target.fileSystemRepresentation, &st) != 0) {
      LRequire(errno == ENOENT, @"Could not inspect existing profile content.");
      if (fillMissing) {
        NSString* source = [resourceRoot stringByAppendingPathComponent:rel];
        LMakeDirectory(target.stringByDeletingLastPathComponent);
        LCopyProfileRecordExclusive(source, target, row);
      }
      continue;
    }
    LRequire(LMatchesProfileRecord(target, row),
             [@"Existing profile content differs; it was left unchanged: " stringByAppendingString:rel]);
  }
}

static bool LPendingReplayImportPresent(NSString* profileRoot) {
  LNoSymlinkPath(profileRoot);
  NSString* journal = [profileRoot stringByAppendingPathComponent:@".futari-replay-import.pending"];
  struct stat st;
  if (lstat(journal.fileSystemRepresentation, &st) == 0) return true;
  LRequire(errno == ENOENT, @"Could not safely inspect replay import recovery state.");
  return false;
}

static void LInstallContent(NSString* bundle, NSString* profileRoot, LProfile profile) {
  struct stat st;
  if (lstat(profileRoot.fileSystemRepresentation, &st) == 0) {
    LRequire(S_ISDIR(st.st_mode) && !S_ISLNK(st.st_mode), @"The selected profile path is unsafe.");
    LCheckProfileFiles(bundle, profileRoot, profile, true);
    return;
  }
  LRequire(errno == ENOENT, @"Could not inspect the selected profile path.");
  NSString* profiles = profileRoot.stringByDeletingLastPathComponent;
  LMakeDirectory(profiles);
  NSString* stage = [profiles stringByAppendingPathComponent:
      [@".profile-stage-" stringByAppendingString:NSUUID.UUID.UUIDString]];
  LRequire(mkdir(stage.fileSystemRepresentation, 0700) == 0, @"Could not create a private profile stage.");
  bool published = false;
  try {
    NSString* resourceRoot = [[bundle stringByAppendingPathComponent:@"Contents/Resources/ContentProfiles"]
        stringByAppendingPathComponent:profile.content];
    for (NSDictionary* row in profile.files) {
      NSString* rel = LSafeMember(row[@"path"]);
      NSString* source = [resourceRoot stringByAppendingPathComponent:rel];
      NSString* target = [stage stringByAppendingPathComponent:rel];
      LMakeDirectory(target.stringByDeletingLastPathComponent);
      LCopyProfileRecordExclusive(source, target, row);
    }
    LRequire(renameatx_np(AT_FDCWD, stage.fileSystemRepresentation, AT_FDCWD,
                          profileRoot.fileSystemRepresentation, RENAME_EXCL) == 0,
             @"The profile appeared during setup; no existing data was replaced.");
    published = true;
  } catch (...) {
    if (!published) {
      NSFileManager* fm = NSFileManager.defaultManager;
      [fm removeItemAtPath:stage error:nil];
    }
    throw;
  }
}

static void LWriteConsoleLine(int fd, NSString* line) {
  NSData* bytes = [[line stringByAppendingString:@"\n"] dataUsingEncoding:NSUTF8StringEncoding];
  if (fd >= 0) { (void)write(fd, bytes.bytes, bytes.length); (void)fsync(fd); }
}

static NSFileHandle* LOpenAppendLog(NSString* path) {
  int fd = open(path.fileSystemRepresentation, O_WRONLY | O_APPEND | O_NOFOLLOW);
  if (fd < 0) return nil;
  NSFileHandle* handle = [[NSFileHandle alloc] initWithFileDescriptor:fd closeOnDealloc:YES];
  if (handle == nil) close(fd);
  return handle;
}

static void LCleanupUnlaunchedTask(NSTask* task, NSFileHandle* output) {
  if (task != nil && task.isRunning) return;
  if (task != nil) {
    task.terminationHandler = nil;
    task.standardOutput = nil;
    task.standardError = nil;
  }
  [output closeFile];
}

#ifdef FUTARI_LAUNCHER_TESTING
static void LWriteSettingsAtomicallyImpl(NSString* path, NSData* bytes, int failPoint) {
#else
static void LWriteSettingsAtomicallyImpl(NSString* path, NSData* bytes) {
#endif
  LNoSymlinkPath(path);
  struct stat st;
  if (lstat(path.fileSystemRepresentation, &st) == 0)
    LRequire(S_ISREG(st.st_mode), @"Settings path is not a regular file; it was left untouched.");
  else LRequire(errno == ENOENT, @"Could not inspect launcher settings.");
  NSString* stage = [path.stringByDeletingLastPathComponent stringByAppendingPathComponent:
      [@".settings-" stringByAppendingString:NSUUID.UUID.UUIDString]];
  int fd = open(stage.fileSystemRepresentation, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
  LRequire(fd >= 0, @"Could not create settings staging file.");
  size_t offset = 0;
  while (offset < bytes.length) {
    ssize_t n = write(fd, (const uint8_t*)bytes.bytes + offset, bytes.length - offset);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) {
      close(fd); unlink(stage.fileSystemRepresentation);
      throw std::runtime_error("Could not write launcher settings.");
    }
    offset += (size_t)n;
  }
#ifdef FUTARI_LAUNCHER_TESTING
  if (failPoint == 1) errno = EIO;
  if (failPoint == 1 || fsync(fd) != 0) {
#else
  if (fsync(fd) != 0) {
#endif
    close(fd); unlink(stage.fileSystemRepresentation);
    throw std::runtime_error("Could not sync launcher settings.");
  }
  close(fd);
#ifdef FUTARI_LAUNCHER_TESTING
  if (failPoint == 2) errno = EACCES;
  if (failPoint == 2 || rename(stage.fileSystemRepresentation, path.fileSystemRepresentation) != 0) {
#else
  if (rename(stage.fileSystemRepresentation, path.fileSystemRepresentation) != 0) {
#endif
    unlink(stage.fileSystemRepresentation);
    throw std::runtime_error("Could not atomically update launcher settings.");
  }
}
static void LWriteSettingsAtomically(NSString* path, NSData* bytes) {
#ifdef FUTARI_LAUNCHER_TESTING
  LWriteSettingsAtomicallyImpl(path, bytes, 0);
#else
  LWriteSettingsAtomicallyImpl(path, bytes);
#endif
}
#ifdef FUTARI_LAUNCHER_TESTING
static void LWriteSettingsAtomicallyForTest(NSString* path, NSData* bytes, int failPoint) {
  LWriteSettingsAtomicallyImpl(path, bytes, failPoint);
}
#endif

@interface FutariLauncherDelegate : NSObject <NSApplicationDelegate, NSWindowDelegate>
@property(nonatomic, strong) NSWindow* window;
@property(nonatomic, strong) NSPopUpButton* content;
@property(nonatomic, strong) NSButton* launchButton;
@property(nonatomic, strong) NSButton* importButton;
@property(nonatomic, strong) NSButton* exportButton;
@property(nonatomic, strong) NSButton* fullscreenToggle;
@property(nonatomic, strong) NSTextField* status;
@property(nonatomic, strong) NSTextField* details;
@property(nonatomic, strong) NSTask* child;
@property(nonatomic, strong) NSTask* helperTask;
@property(nonatomic, strong) NSString* supportRoot;
@property(nonatomic, strong) NSString* profileRoot;
@property(nonatomic, strong) NSString* exchangeRoot;
@property(nonatomic, strong) NSString* logsRoot;
@property(nonatomic, strong) NSString* activeLog;
@property(nonatomic, strong) NSString* activeContent;
@property(nonatomic) int lockFD;
@property(nonatomic) BOOL diagnostic;
- (void)runHelper:(NSArray<NSString*>*)arguments label:(NSString*)label;
@end

@implementation FutariLauncherDelegate
- (void)applicationDidFinishLaunching:(NSNotification*)note {
  self.lockFD = -1;
  NSString* home = NSHomeDirectory();
  const char* override = getenv("FUTARI_PACKAGE_USER_DATA");
  const char* exchangeOverride = getenv("FUTARI_PACKAGE_REPLAY_EXCHANGE");
  const char* diagnostic = getenv("FUTARI_DIAGNOSTIC_AUTOSTART");
  self.diagnostic = diagnostic && strcmp(diagnostic, "1") == 0;
  if ((override && !LAbsoluteOverride(override)) ||
      (exchangeOverride && !LAbsoluteOverride(exchangeOverride)) ||
      (self.diagnostic && !LAbsoluteOverride(override))) {
    [self showFatal:@"Test overrides must be nonempty absolute paths. Diagnostic launch also requires FUTARI_PACKAGE_USER_DATA; refusing to use player data."];
    return;
  }
  self.supportRoot = override && override[0] == '/' ? [NSString stringWithUTF8String:override] :
      [home stringByAppendingPathComponent:@"Library/Application Support/Mushihimesama Futari"];
  self.exchangeRoot = exchangeOverride ? [NSString stringWithUTF8String:exchangeOverride] :
      (override ? [self.supportRoot stringByAppendingPathComponent:@"Replay Exchange"] :
       [home stringByAppendingPathComponent:@"Desktop/360 Recomp/Replays"]);
  self.logsRoot = [self.supportRoot stringByAppendingPathComponent:@"Logs"];
  bool fullscreen = false;
  NSString* settingsWarning = nil;
  NSString* settingsPath = [self.supportRoot stringByAppendingPathComponent:@"launcher-settings.json"];
  struct stat settingsStat;
  try {
    if (lstat(settingsPath.fileSystemRepresentation, &settingsStat) == 0) {
      NSData* settingsData = LReadRegular(settingsPath, 65536);
      id settingsValue = nil;
      @try { settingsValue = [NSJSONSerialization JSONObjectWithData:settingsData options:0 error:nil]; }
      @catch (NSException* exception) { (void)exception; }
      if ([settingsValue isKindOfClass:NSDictionary.class] &&
          LJSONBool(((NSDictionary*)settingsValue)[@"fullscreen"], YES)) fullscreen = true;
      else if ([settingsValue isKindOfClass:NSDictionary.class] &&
               LJSONBool(((NSDictionary*)settingsValue)[@"fullscreen"], NO)) fullscreen = false;
      else settingsWarning = @"Launcher settings are invalid; defaults are active. Saving a setting will replace only this settings file.";
    } else LRequire(errno == ENOENT, @"Could not inspect launcher settings.");
  } catch (const std::exception& e) { settingsWarning = [NSString stringWithUTF8String:e.what()]; }
  self.content = [[NSPopUpButton alloc] initWithFrame:NSMakeRect(0, 0, 360, 30) pullsDown:NO];
  [self.content addItemsWithTitles:@[@"Base game", @"Ver 1.01", @"Black Label", @"Both add-ons"]];
  [self.content setTarget:self]; [self.content setAction:@selector(contentChanged:)];
  self.launchButton = [self button:@"Play" action:@selector(launchGame:) frame:NSMakeRect(24, 278, 120, 34)];
  self.importButton = [self button:@"Import replay…" action:@selector(importReplay:) frame:NSMakeRect(158, 278, 142, 34)];
  self.exportButton = [self button:@"Export replays…" action:@selector(exportReplays:) frame:NSMakeRect(316, 278, 150, 34)];
  self.status = [NSTextField labelWithString:@"Choose content, then press Play. Replay transfer is explicit; nothing is imported or exported automatically."];
  self.status.frame = NSMakeRect(24, 196, 500, 44); self.status.lineBreakMode = NSLineBreakByWordWrapping;
  self.details = [NSTextField labelWithString:@""];
  self.details.frame = NSMakeRect(24, 166, 500, 24); self.details.textColor = NSColor.secondaryLabelColor;
  self.fullscreenToggle = [NSButton checkboxWithTitle:@"Start in fullscreen" target:self action:@selector(settingsChanged:)];
  self.fullscreenToggle.frame = NSMakeRect(24, 246, 230, 24);
  self.fullscreenToggle.state = fullscreen ? NSControlStateValueOn : NSControlStateValueOff;
  NSButton* saves = [self button:@"Show saves" action:@selector(showSaves:) frame:NSMakeRect(24, 24, 110, 30)];
  NSButton* logs = [self button:@"Show logs" action:@selector(showLogs:) frame:NSMakeRect(146, 24, 100, 30)];
  NSButton* exchange = [self button:@"Show replay folder" action:@selector(showExchange:) frame:NSMakeRect(258, 24, 160, 30)];
  (void)saves; (void)logs; (void)exchange;
  NSTextField* label = [NSTextField labelWithString:@"Game content"];
  label.frame = NSMakeRect(24, 350, 150, 22);
  self.content.frame = NSMakeRect(24, 316, 360, 28);
  self.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 540, 390)
      styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
      backing:NSBackingStoreBuffered defer:NO];
  self.window.title = @"Mushihimesama Futari"; self.window.delegate = self;
  [self.window.contentView addSubview:label]; [self.window.contentView addSubview:self.content];
  [self.window.contentView addSubview:self.launchButton]; [self.window.contentView addSubview:self.importButton];
  [self.window.contentView addSubview:self.exportButton]; [self.window.contentView addSubview:self.status];
  [self.window.contentView addSubview:self.details]; [self.window.contentView addSubview:self.fullscreenToggle];
  [self.window.contentView addSubview:saves]; [self.window.contentView addSubview:logs]; [self.window.contentView addSubview:exchange];
  [self.window center]; [self.window makeKeyAndOrderFront:nil];
  NSMenu* menu = [[NSMenu alloc] initWithTitle:@"Main Menu"];
  NSMenuItem* appItem = [[NSMenuItem alloc] initWithTitle:@"Mushihimesama Futari" action:nil keyEquivalent:@""];
  [menu addItem:appItem];
  NSMenu* appMenu = [[NSMenu alloc] initWithTitle:@"Mushihimesama Futari"];
  [appMenu addItemWithTitle:@"Quit Mushihimesama Futari" action:@selector(terminate:) keyEquivalent:@"q"];
  appItem.submenu = appMenu; NSApp.mainMenu = menu;
  if (settingsWarning) self.status.stringValue = settingsWarning;
  [NSApp activateIgnoringOtherApps:YES];
  [self refreshProfile];
  if (self.diagnostic) [self launchGame:nil];
}
- (NSButton*)button:(NSString*)title action:(SEL)action frame:(NSRect)frame {
  NSButton* b = [NSButton buttonWithTitle:title target:self action:action]; b.frame = frame; return b;
}
- (void)showFatal:(NSString*)message {
  NSAlert* alert = [[NSAlert alloc] init]; alert.messageText = @"Mushihimesama Futari setup stopped safely";
  alert.informativeText = message; [alert addButtonWithTitle:@"Quit"]; [alert runModal]; [NSApp terminate:nil];
}
- (NSString*)contentId {
  switch (self.content.indexOfSelectedItem) {
    case 1: return @"ver101"; case 2: return @"blacklabel"; case 3: return @"both"; default: return @"base";
  }
}
- (void)contentChanged:(id)sender { if (!self.child.isRunning && !self.helperTask.isRunning) [self refreshProfile]; }
- (void)settingsChanged:(id)sender {
  if (self.child.isRunning || self.helperTask.isRunning) return;
  try {
    LMakeDirectory(self.supportRoot);
    NSError* error = nil;
    NSData* bytes = [NSJSONSerialization dataWithJSONObject:@{@"schema_version": @1,
      @"fullscreen": @(self.fullscreenToggle.state == NSControlStateValueOn)}
      options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys error:&error];
    LRequire(bytes != nil, @"Could not encode launcher settings.");
    NSString* path = [self.supportRoot stringByAppendingPathComponent:@"launcher-settings.json"];
    LWriteSettingsAtomically(path, bytes);
    self.status.stringValue = @"Launcher setting saved outside the app bundle.";
  } catch (const std::exception& e) { self.status.stringValue = [NSString stringWithUTF8String:e.what()]; }
}
- (void)refreshProfile {
  try {
    NSString* content = [self contentId];
    NSString* bundle = NSBundle.mainBundle.bundlePath;
    LProfile p = LLoadProfile(bundle, content);
    LValidatePackageAndProfile(bundle, p);
    self.profileRoot = [[self.supportRoot stringByAppendingPathComponent:@"Profiles"]
        stringByAppendingPathComponent:content];
    self.activeContent = content;
    self.details.stringValue = [NSString stringWithFormat:@"Profile: %@  •  Build: %@…", content,
                                [p.build substringToIndex:12]];
    self.status.stringValue = @"Content manifest and packaged files verified. Existing saves will not be migrated or replaced.";
    [self updateControls];
  } catch (const std::exception& e) {
    self.status.stringValue = [NSString stringWithUTF8String:e.what()];
    self.launchButton.enabled = NO; self.importButton.enabled = NO; self.exportButton.enabled = NO;
  }
}
- (void)updateControls {
  BOOL running = self.child.isRunning || self.helperTask.isRunning;
  self.content.enabled = !running;
  self.launchButton.enabled = !running;
  self.importButton.enabled = !running;
  self.exportButton.enabled = !running;
  self.fullscreenToggle.enabled = !running;
}
- (void)launchGame:(id)sender {
  if (self.child.isRunning || self.helperTask.isRunning) return;
  int consoleFD = -1;
  NSTask* launchTask = nil;
  NSFileHandle* childConsole = nil;
  try {
    NSString* bundle = NSBundle.mainBundle.bundlePath;
    NSString* content = [self contentId];
    LProfile profile = LLoadProfile(bundle, content);
    LValidatePackageAndProfile(bundle, profile);
    NSString* game = [bundle stringByAppendingPathComponent:@"Contents/MacOS/futari15x360"];
    NSString* gameData = [bundle stringByAppendingPathComponent:@"Contents/Resources/GameData"];
    NSString* helper = [bundle stringByAppendingPathComponent:@"Contents/MacOS/FutariReplayBundle"];
    struct stat st;
    LRequire(lstat(game.fileSystemRepresentation, &st) == 0 && S_ISREG(st.st_mode) && (st.st_mode & 0111),
             @"Game executable is missing or not executable.");
    LRequire(lstat(helper.fileSystemRepresentation, &st) == 0 && S_ISREG(st.st_mode) && (st.st_mode & 0111),
             @"The native replay helper is missing; this app package is incomplete.");
    LRequire([[NSFileManager defaultManager] fileExistsAtPath:[gameData stringByAppendingPathComponent:@"default.xex"]],
             @"The app package is missing its game data.");
    LMakeDirectory(self.supportRoot); LMakeDirectory(self.logsRoot);
    LMakeDirectory(self.profileRoot.stringByDeletingLastPathComponent);
    LMakeDirectory(self.profileRoot);
    NSString* lock = [self.profileRoot stringByAppendingPathComponent:@".futari-profile.lock"];
    int lockFD = open(lock.fileSystemRepresentation, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600);
    LRequire(lockFD >= 0, @"Could not open the selected profile lock.");
    struct stat lockStat;
    if (fstat(lockFD, &lockStat) != 0 || !S_ISREG(lockStat.st_mode)) {
      close(lockFD);
      throw std::runtime_error("The selected profile lock is not a regular file.");
    }
    if (flock(lockFD, LOCK_EX | LOCK_NB) != 0) { close(lockFD); throw std::runtime_error("This profile is already in use."); }
    self.lockFD = lockFD;
    if (LPendingReplayImportPresent(self.profileRoot)) {
      flock(self.lockFD, LOCK_UN); close(self.lockFD); self.lockFD = -1;
      self.status.stringValue = @"An interrupted replay import was found. Recovery is running; the game will stay closed. Press Play after recovery finishes.";
      [self runHelper:@[@"--recover"] label:@"Replay import recovery"];
      return;
    }
    LInstallContent(bundle, self.profileRoot, profile);
    LCheckProfileFiles(bundle, self.profileRoot, profile, true);
    NSString* session = NSUUID.UUID.UUIDString;
    self.activeLog = [self.logsRoot stringByAppendingPathComponent:[session stringByAppendingString:@".log"]];
    NSString* consolePath = [self.logsRoot stringByAppendingPathComponent:[session stringByAppendingString:@"-console.log"]];
    consoleFD = open(consolePath.fileSystemRepresentation, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    LRequire(consoleFD >= 0, @"Could not create a new session log.");
    LWriteConsoleLine(consoleFD, [NSString stringWithFormat:@"launcher: content=%@ build=%@ profile=%@", content,
                                  profile.build, self.profileRoot]);
    NSMutableDictionary* env = [NSProcessInfo.processInfo.environment mutableCopy];
    [env removeObjectForKey:@"FUTARI_TEST_DIR"]; [env removeObjectForKey:@"DYLD_LIBRARY_PATH"];
    [env removeObjectForKey:@"DYLD_FALLBACK_LIBRARY_PATH"];
    [env removeObjectForKey:@"FUTARI_DIAGNOSTIC_AUTOSTART"];
    [env setObject:self.profileRoot forKey:@"FUTARI_PACKAGE_USER_DATA"];
    [env setObject:@"1" forKey:@"MVK_CONFIG_LOG_LEVEL"];
    NSString* icd = [bundle stringByAppendingPathComponent:@"Contents/Resources/vulkan/share/vulkan/icd.d/MoltenVK_icd.json"];
    [env setObject:icd forKey:@"VK_DRIVER_FILES"]; [env setObject:icd forKey:@"VK_ICD_FILENAMES"];
    NSMutableArray* args = [NSMutableArray arrayWithArray:@[
      [@"--game_data_root=" stringByAppendingString:gameData],
      [@"--user_data_root=" stringByAppendingString:self.profileRoot],
      [@"--cache_root=" stringByAppendingString:[self.profileRoot stringByAppendingPathComponent:@"cache"]],
      [@"--log_file=" stringByAppendingString:self.activeLog], @"--log_level=info", @"--gpu_plugin=xenos",
      @"--protect_zero=false", [NSString stringWithFormat:@"--fullscreen=%@",
          self.fullscreenToggle.state == NSControlStateValueOn ? @"true" : @"false"],
      @"--window_width=960", @"--window_height=540", @"--mnk_mode=false",
      @"--futari_replay_saved_game_compat=true"]];
    (void)args;
    NSTask* task = [[NSTask alloc] init]; launchTask = task;
    task.executableURL = [NSURL fileURLWithPath:game];
    task.arguments = args; task.environment = env; task.currentDirectoryURL = [NSURL fileURLWithPath:self.profileRoot];
    childConsole = LOpenAppendLog(consolePath);
    LRequire(childConsole != nil, @"Could not open the append-only child session log.");
    task.standardOutput = childConsole; task.standardError = childConsole;
    self.child = task; self.activeContent = content;
    __weak FutariLauncherDelegate* weakSelf = self;
    task.terminationHandler = ^(NSTask* finished) { dispatch_async(dispatch_get_main_queue(), ^{
      [childConsole synchronizeFile]; [childConsole closeFile];
      FutariLauncherDelegate* strongSelf = weakSelf; if (!strongSelf) return;
      if (strongSelf.lockFD >= 0) { flock(strongSelf.lockFD, LOCK_UN); close(strongSelf.lockFD); strongSelf.lockFD = -1; }
      NSString* receiptPath = [strongSelf.activeLog stringByAppendingString:@".title-termination-receipt.json"];
      NSDictionary* receipt = nil;
      try {
        NSData* data = LReadRegular(receiptPath, 65536);
        id parsed = nil;
        @try { parsed = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil]; }
        @catch (NSException* exception) { (void)exception; }
        if ([parsed isKindOfClass:NSDictionary.class]) receipt = parsed;
      } catch (const std::exception&) {}
      BOOL titleReturned = LJSONBool(receipt[@"title_termination_returned"], YES);
      BOOL flushAttempted = LJSONBool(receipt[@"log_flush_attempted"], YES);
      BOOL hardExit = LJSONBool(receipt[@"hard_exit"], YES);
      BOOL normalRequested = LJSONBool(receipt[@"normal_requested_close"], YES);
      BOOL receiptValid = LJSONInteger(receipt[@"schema_version"], 1) &&
          LJSONInteger(receipt[@"process_id"], finished.processIdentifier) &&
          LJSONBool(receipt[@"clean_exit"], NO);
      BOOL waitOK = finished.terminationReason == NSTaskTerminationReasonExit && finished.terminationStatus == 0;
      NSString* exitReason = finished.terminationReason == NSTaskTerminationReasonExit ? @"exit" : @"signal";
      int exitFD = open(consolePath.fileSystemRepresentation, O_WRONLY | O_APPEND | O_NOFOLLOW);
      LWriteConsoleLine(exitFD, [NSString stringWithFormat:@"FUTARI_DIAGNOSTIC_CHILD_EXIT pid=%d reason=%@ status=%d",
                                  (int)finished.processIdentifier, exitReason, (int)finished.terminationStatus]);
      if (exitFD >= 0) close(exitFD);
      NSString* qualification = waitOK && receiptValid && titleReturned && flushAttempted && hardExit && normalRequested ?
          @"Requested Quit recorded; title termination returned, log flush was attempted, and the child exited 0. Guest-thread drainage is not certified." :
          [NSString stringWithFormat:@"Child ended (status %d). Close/flush receipt is missing or incomplete; review the session log.", (int)finished.terminationStatus];
      strongSelf.status.stringValue = qualification;
      strongSelf.details.stringValue = [NSString stringWithFormat:@"Session log: %@", strongSelf.activeLog];
      [strongSelf updateControls];
    }); };
    NSError* error = nil;
    // The game executable acquires and retains this exact profile lock before
    // opening its runtime/config/save paths. Release only after content setup;
    // if a transfer wins the small launch race, the child fails closed.
    flock(self.lockFD, LOCK_UN); close(self.lockFD); self.lockFD = -1;
    LRequire([task launchAndReturnError:&error], error.localizedDescription ?: @"Could not start the game process.");
    close(consoleFD); consoleFD = -1;
    self.status.stringValue = @"Game is running. Close the game window to return here; this window will wait for that exact process before enabling replay transfer.";
    self.details.stringValue = [NSString stringWithFormat:@"Game process %d  •  %@", (int)task.processIdentifier, self.activeLog];
    [self updateControls];
    if (self.diagnostic) {
      NSString* marker = [NSString stringWithFormat:@"FUTARI_DIAGNOSTIC_CHILD_STARTED pid=%d content=%@ log=%@ (process start only; not guest-init proof)",
                          (int)task.processIdentifier, content, self.activeLog];
      int markerFD = open(consolePath.fileSystemRepresentation, O_WRONLY | O_APPEND | O_NOFOLLOW);
      LWriteConsoleLine(markerFD, marker);
      if (markerFD >= 0) close(markerFD);
    }
  } catch (const std::exception& e) {
    if (consoleFD >= 0) close(consoleFD);
    LCleanupUnlaunchedTask(launchTask, childConsole);
    if (launchTask != nil && !launchTask.isRunning) {
      if (self.child == launchTask) self.child = nil;
    }
    if (self.lockFD >= 0) { flock(self.lockFD, LOCK_UN); close(self.lockFD); self.lockFD = -1; }
    self.status.stringValue = [NSString stringWithUTF8String:e.what()];
  }
  [self updateControls];
}
- (void)runHelper:(NSArray<NSString*>*)arguments label:(NSString*)label {
  if (self.child.isRunning || self.helperTask.isRunning) return;
  NSTask* helperTask = nil;
  NSFileHandle* helperOutput = nil;
  try {
    LMakeDirectory(self.supportRoot);
    LMakeDirectory(self.logsRoot);
    NSString* bundle = NSBundle.mainBundle.bundlePath;
    NSString* helper = [bundle stringByAppendingPathComponent:@"Contents/MacOS/FutariReplayBundle"];
    LProfile profile = LLoadProfile(bundle, [self contentId]); LValidatePackageAndProfile(bundle, profile);
    NSTask* task = [[NSTask alloc] init]; helperTask = task;
    task.executableURL = [NSURL fileURLWithPath:helper];
    NSMutableArray* args = [NSMutableArray arrayWithArray:arguments];
    if ([arguments.firstObject isEqual:@"--recover"]) {
      LRequire(arguments.count == 1, @"Replay recovery does not take additional options.");
      args = [NSMutableArray arrayWithObject:@"--recover"];
      [args addObjectsFromArray:@[self.profileRoot, profile.content, profile.build]];
    } else if ([arguments.firstObject isEqual:@"--export"]) {
      LRequire(arguments.count == 2, @"Export requires one destination folder.");
      args = [NSMutableArray arrayWithObject:@"--export"];
      [args addObjectsFromArray:@[self.profileRoot, arguments[1], profile.content, profile.build]];
    } else {
      LRequire(arguments.count == 2 && [arguments[0] isEqual:@"--import"], @"Import requires one selected archive.");
      [args addObjectsFromArray:@[self.profileRoot, profile.content, profile.build]];
    }
    task.arguments = args; task.currentDirectoryURL = [NSURL fileURLWithPath:self.supportRoot];
    NSString* output = [self.logsRoot stringByAppendingPathComponent:
        [NSString stringWithFormat:@"%@-%@.log", NSUUID.UUID.UUIDString, label]];
    LRequire([[NSFileManager defaultManager] createFileAtPath:output contents:[NSData data] attributes:@{NSFilePosixPermissions:@0600}],
             @"Could not create transfer log.");
    helperOutput = LOpenAppendLog(output);
    LRequire(helperOutput != nil, @"Could not open the append-only transfer log.");
    task.standardOutput = helperOutput; task.standardError = helperOutput;
    self.helperTask = task;
    __weak FutariLauncherDelegate* weakSelf = self;
    task.terminationHandler = ^(NSTask* finished) { dispatch_async(dispatch_get_main_queue(), ^{
      [helperOutput synchronizeFile]; [helperOutput closeFile];
      FutariLauncherDelegate* strongSelf = weakSelf; if (!strongSelf) return;
      if (strongSelf.helperTask == finished) strongSelf.helperTask = nil;
      if ([label isEqual:@"Replay import recovery"]) {
        strongSelf.status.stringValue = finished.terminationStatus == 0 ?
            [NSString stringWithFormat:@"Replay import recovery completed. The game was not launched; press Play when ready. Log: %@", output] :
            [NSString stringWithFormat:@"Replay import recovery stopped safely (status %d). The game was not launched; see %@", (int)finished.terminationStatus, output];
      } else {
        strongSelf.status.stringValue = finished.terminationStatus == 0 ?
            [NSString stringWithFormat:@"%@ completed. Review the transfer log at %@", label, output] :
            [NSString stringWithFormat:@"%@ stopped safely (status %d). Existing saves were not replaced; see %@", label, (int)finished.terminationStatus, output];
      }
      strongSelf.details.stringValue = @"Replay archives are integrity-checked transfers; this does not certify recording or playback.";
      [strongSelf updateControls];
    }); };
    NSError* error = nil; LRequire([task launchAndReturnError:&error], error.localizedDescription ?: @"Could not start native replay transfer.");
    self.status.stringValue = [NSString stringWithFormat:@"%@ in progress…", label];
    [self updateControls];
  } catch (const std::exception& e) {
    LCleanupUnlaunchedTask(helperTask, helperOutput);
    if (helperTask != nil && !helperTask.isRunning) {
      if (self.helperTask == helperTask) self.helperTask = nil;
    }
    self.status.stringValue = [NSString stringWithUTF8String:e.what()];
    [self updateControls];
  }
}
- (void)importReplay:(id)sender {
  if (self.child.isRunning) return;
  try {
    NSString* imports = [self.exchangeRoot stringByAppendingPathComponent:@"Imports"];
    LMakeDirectory(imports);
    NSOpenPanel* panel = [NSOpenPanel openPanel]; panel.canChooseFiles = YES; panel.canChooseDirectories = NO;
    panel.allowsMultipleSelection = NO; panel.allowedFileTypes = @[@"futari-replay"];
    panel.directoryURL = [NSURL fileURLWithPath:imports isDirectory:YES];
    if ([panel runModal] != NSModalResponseOK) return;
    [self runHelper:@[@"--import", panel.URL.path] label:@"Replay import"];
  } catch (const std::exception& e) { self.status.stringValue = [NSString stringWithUTF8String:e.what()]; }
}
- (void)exportReplays:(id)sender {
  if (self.child.isRunning) return;
  try {
    NSString* exports = [self.exchangeRoot stringByAppendingPathComponent:@"Exports"];
    LMakeDirectory(exports);
    NSOpenPanel* panel = [NSOpenPanel openPanel]; panel.canChooseFiles = NO; panel.canChooseDirectories = YES;
    panel.canCreateDirectories = YES; panel.prompt = @"Export here"; panel.directoryURL = [NSURL fileURLWithPath:exports isDirectory:YES];
    if ([panel runModal] != NSModalResponseOK) return;
    [self runHelper:@[@"--export", panel.URL.path] label:@"Replay export"];
  } catch (const std::exception& e) { self.status.stringValue = [NSString stringWithUTF8String:e.what()]; }
}
- (void)reveal:(NSString*)path {
  try { LMakeDirectory(path); [[NSWorkspace sharedWorkspace] openURL:[NSURL fileURLWithPath:path isDirectory:YES]]; }
  catch (const std::exception& e) { self.status.stringValue = [NSString stringWithUTF8String:e.what()]; }
}
- (void)showSaves:(id)sender { [self reveal:self.profileRoot ?: self.supportRoot]; }
- (void)showLogs:(id)sender { [self reveal:self.logsRoot]; }
- (void)showExchange:(id)sender { [self reveal:self.exchangeRoot]; }
- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication*)sender {
  if (self.child.isRunning || self.helperTask.isRunning) {
    NSAlert* alert = [[NSAlert alloc] init]; alert.messageText = self.child.isRunning ? @"The game is still running" : @"Replay transfer is still running";
    alert.informativeText = self.child.isRunning ? @"Close the game window first. The launcher must keep supervising its exact process and profile until it exits." : @"Wait for the current native replay transfer to finish before quitting.";
    [alert addButtonWithTitle:@"Keep launcher open"]; [alert runModal];
    return NSTerminateCancel;
  }
  return NSTerminateNow;
}
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication*)sender { return YES; }
- (BOOL)windowShouldClose:(NSWindow*)sender {
  if (self.child.isRunning || self.helperTask.isRunning) {
    self.status.stringValue = self.child.isRunning ? @"The game is still running. Close its game window first; the launcher will remain open and keep supervising it." : @"Replay transfer is still running; wait before closing the launcher.";
    return NO;
  }
  return YES;
}
@end

static int runLauncherApp() {
  @autoreleasepool {
    NSApplication* app = [NSApplication sharedApplication];
    app.activationPolicy = NSApplicationActivationPolicyRegular;
    FutariLauncherDelegate* delegate = [[FutariLauncherDelegate alloc] init];
    app.delegate = delegate;
    [app run];
    return 0;
  }
}
