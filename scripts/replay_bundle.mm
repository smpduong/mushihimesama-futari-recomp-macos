// Native, explicit replay-package import/export for the packaged macOS app.
// This handles file integrity and transfer only; it does not certify playback.
#import <Foundation/Foundation.h>
#import "libarchive_api_subset.h"
#include <CommonCrypto/CommonDigest.h>
#include <CoreFoundation/CoreFoundation.h>
#include <sys/file.h>
#include <sys/stdio.h>
#include <sys/stat.h>
#include <limits.h>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <functional>
#include <memory>
#include <cerrno>
#include <algorithm>
#include <cctype>
#include <cstdint>
#include <iostream>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace futari_replay {
constexpr uint64_t kArchiveLimit = 128ull * 1024 * 1024;
constexpr uint64_t kPayloadLimit = 128ull * 1024 * 1024;
constexpr size_t kMaxFiles = 128;
constexpr char kTitle[] = "435A07D7";
constexpr char kProfile[] = "B13EBABEBABEBABE";
constexpr char kFormat[] = "futari-port-replay-1";
constexpr char kExtension[] = ".futari-replay";
constexpr char kPendingJournal[] = ".futari-replay-import.pending";
constexpr char kJournalSchema[] = "futari-replay-import-transaction-1";
using Bytes = std::vector<uint8_t>;
using FileMap = std::map<std::string, Bytes>;
struct FileIdentity { uint64_t device; uint64_t inode; };

static void need(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}
static dirent* nextDirectoryEntry(DIR* dir, const std::string& path) {
  errno = 0;
  dirent* entry = readdir(dir);
  need(entry != nullptr || errno == 0, "Cannot read directory: " + path);
  return entry;
}
class ScopedFD {
 public:
  explicit ScopedFD(int fd = -1) : fd_(fd) {}
  ~ScopedFD() { if (fd_ >= 0) close(fd_); }
  ScopedFD(const ScopedFD&) = delete;
  ScopedFD& operator=(const ScopedFD&) = delete;
  int get() const { return fd_; }
 private:
  int fd_;
};
static std::string str(NSString* value) {
  const char* bytes = value.UTF8String;
  need(bytes != nullptr, "Invalid UTF-8 string");
  return bytes;
}
static NSString* ns(const std::string& value) {
  NSString* result = [[NSString alloc] initWithBytes:value.data() length:value.size()
                                               encoding:NSUTF8StringEncoding];
  need(result != nil, "Invalid UTF-8 string");
  return result;
}
static std::string pathString(NSString* value) { return str(value.stringByStandardizingPath); }

static void ensureNoSymlink(const std::string& path) {
  need(!path.empty() && path[0] == '/', "Expected absolute path");
  std::string component;
  size_t pos = 1;
  while (pos <= path.size()) {
    size_t end = path.find('/', pos);
    if (end == std::string::npos) end = path.size();
    std::string part = path.substr(pos, end - pos);
    if (!part.empty()) {
      need(part != "." && part != "..", "Unsafe path component");
      component += "/" + part;
      struct stat info;
      if (lstat(component.c_str(), &info) == 0)
        need(!S_ISLNK(info.st_mode), "Symlink path rejected: " + component);
      else
        need(errno == ENOENT, "Cannot inspect path: " + component);
    }
    if (end == path.size()) break;
    pos = end + 1;
  }
}

static Bytes readRegular(const std::string& path, uint64_t limit,
                         const FileIdentity* expectedIdentity = nullptr,
                         FileIdentity* actualIdentity = nullptr) {
  ensureNoSymlink(path);
  int fd = open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
  need(fd >= 0, "Cannot open file: " + path);
  struct stat info;
  if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_size < 0 ||
      static_cast<uint64_t>(info.st_size) > limit) {
    close(fd);
    throw std::runtime_error("Not a regular bounded file: " + path);
  }
  FileIdentity actual{static_cast<uint64_t>(info.st_dev), static_cast<uint64_t>(info.st_ino)};
  if (expectedIdentity != nullptr && (actual.device != expectedIdentity->device ||
                                      actual.inode != expectedIdentity->inode)) {
    close(fd);
    throw std::runtime_error("File ownership identity changed: " + path);
  }
  if (actualIdentity != nullptr) *actualIdentity = actual;
  Bytes result(static_cast<size_t>(info.st_size));
  size_t offset = 0;
  while (offset < result.size()) {
    ssize_t n = read(fd, result.data() + offset, result.size() - offset);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) { close(fd); throw std::runtime_error("Short read: " + path); }
    offset += static_cast<size_t>(n);
  }
  close(fd);
  return result;
}

static void writeExclusive(const std::string& path, const Bytes& bytes) {
  ensureNoSymlink(path);
  std::string parent = path.substr(0, path.find_last_of('/'));
  std::string leaf = path.substr(path.find_last_of('/') + 1);
  int parentFD = open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
  need(parentFD >= 0, "Cannot open staged file parent: " + parent);
  int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
  if (fd < 0) {
    close(parentFD);
    throw std::runtime_error("Refusing to overwrite: " + path);
  }
  struct stat owned;
  if (fstat(fd, &owned) != 0 || !S_ISREG(owned.st_mode)) {
    close(fd);
    close(parentFD);
    throw std::runtime_error("Cannot establish ownership of staged file: " + path);
  }
  auto removeOwnedLeaf = [&]() {
    struct stat current;
    if (fstatat(parentFD, leaf.c_str(), &current, AT_SYMLINK_NOFOLLOW) == 0 &&
        S_ISREG(current.st_mode) && current.st_dev == owned.st_dev && current.st_ino == owned.st_ino)
      unlinkat(parentFD, leaf.c_str(), 0);
  };
  size_t offset = 0;
  while (offset < bytes.size()) {
    ssize_t n = write(fd, bytes.data() + offset, bytes.size() - offset);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) {
      close(fd); removeOwnedLeaf(); close(parentFD);
      throw std::runtime_error("Write failed: " + path);
    }
    offset += static_cast<size_t>(n);
  }
  if (fsync(fd) != 0) {
    close(fd); removeOwnedLeaf(); close(parentFD);
    throw std::runtime_error("Sync failed: " + path);
  }
  close(fd);
  close(parentFD);
}

static std::string sha256(const Bytes& bytes) {
  uint8_t hash[CC_SHA256_DIGEST_LENGTH];
  CC_SHA256(bytes.data(), static_cast<CC_LONG>(bytes.size()), hash);
  static const char hex[] = "0123456789abcdef";
  std::string out;
  out.reserve(sizeof(hash) * 2);
  for (uint8_t b : hash) { out.push_back(hex[b >> 4]); out.push_back(hex[b & 15]); }
  return out;
}

static bool validBuild(const std::string& value) {
  return value.size() == 64 && std::all_of(value.begin(), value.end(), [](unsigned char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
  });
}
static bool validJSONSize(id value, uint64_t expected) {
  if (![value isKindOfClass:NSNumber.class]) return false;
  NSNumber* number = value;
  const char* type = number.objCType;
  bool integer = strcmp(type, @encode(int)) == 0 || strcmp(type, @encode(long)) == 0 ||
      strcmp(type, @encode(long long)) == 0 || strcmp(type, @encode(unsigned int)) == 0 ||
      strcmp(type, @encode(unsigned long)) == 0 || strcmp(type, @encode(unsigned long long)) == 0;
  return integer && number.longLongValue >= 0 && number.unsignedLongLongValue == expected;
}
static bool validContent(const std::string& value) {
  return value == "base" || value == "ver101" || value == "blacklabel" || value == "both";
}
static bool validMember(const std::string& name) {
  if (name.empty() || name.size() > 240 || name[0] == '/' || name[0] == '.') return false;
  if (name.find('\\') != std::string::npos || name.find(':') != std::string::npos ||
      name.find('\0') != std::string::npos) return false;
  for (unsigned char c : name) if (c < 32) return false;
  size_t pos = 0;
  while (pos <= name.size()) {
    size_t end = name.find('/', pos);
    if (end == std::string::npos) end = name.size();
    std::string part = name.substr(pos, end - pos);
    if (part.empty() || part == "." || part == ".." || part[0] == '.') return false;
    if (end == name.size()) break;
    pos = end + 1;
  }
  return true;
}
static bool replayPayload(const std::string& member) {
  const size_t slash = member.find_last_of('/');
  std::string leaf = member.substr(slash == std::string::npos ? 0 : slash + 1);
  std::transform(leaf.begin(), leaf.end(), leaf.begin(), [](unsigned char c) { return std::tolower(c); });
  if (leaf.size() < 14 || leaf.rfind("replaydata", 0) != 0 ||
      leaf.substr(leaf.size() - 4) != ".bin") return false;
  for (size_t i = 10; i + 4 < leaf.size(); ++i)
    if (!std::isdigit(static_cast<unsigned char>(leaf[i]))) return false;
  return true;
}
static void validatePackageName(const std::string& package) {
  need(validMember(package), "Invalid native package name");
  need(package.size() <= 42 && package.find('/') == std::string::npos,
       "Invalid native package name");
  for (unsigned char c : package) need(c > 0 && c < 128, "Package name must be ASCII");
}
static void validateHeader(const Bytes& header, const std::string& package) {
  need(header.size() == 0x148 || header.size() == 0x14c, "Malformed native save header");
  auto be32 = [&](size_t p) { return (uint32_t(header[p]) << 24) | (uint32_t(header[p+1]) << 16) |
                                     (uint32_t(header[p+2]) << 8) | uint32_t(header[p+3]); };
  uint64_t xuid = 0;
  for (size_t i = 0x138; i < 0x140; ++i) xuid = (xuid << 8) | header[i];
  need(be32(4) == 1, "Only saved-game content is accepted; DLC is rejected");
  need(be32(0x140) == 0x435a07d7 || be32(0x140) == 0xffffffff, "Header belongs to another title");
  need(xuid == 0 || xuid == 0xb13ebabebabebabeull || xuid == UINT64_MAX,
       "Header belongs to another profile");
  std::string name(reinterpret_cast<const char*>(header.data() + 0x108), 42);
  size_t nul = name.find('\0');
  if (nul != std::string::npos) name.resize(nul);
  need(name == package, "Header/package filename mismatch");
}

static NSDictionary* parseJSON(const Bytes& bytes) {
  NSError* error = nil;
  NSData* data = [NSData dataWithBytes:bytes.data() length:bytes.size()];
  id result = [NSJSONSerialization JSONObjectWithData:data options:NSJSONReadingFragmentsAllowed error:&error];
  need([result isKindOfClass:NSDictionary.class], "Invalid replay manifest JSON");
  return result;
}
static std::string jsonString(NSDictionary* d, NSString* key) {
  id value = d[key];
  need([value isKindOfClass:NSString.class], "Invalid manifest field: " + str(key));
  return str(value);
}
static Bytes encodeJSON(id object) {
  NSError* error = nil;
  NSData* data = [NSJSONSerialization dataWithJSONObject:object
      options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys error:&error];
  need(data != nil, "Could not encode manifest JSON");
  Bytes bytes(static_cast<const uint8_t*>(data.bytes), static_cast<const uint8_t*>(data.bytes) + data.length);
  bytes.push_back('\n');
  return bytes;
}

static void makeDirs(const std::string& path);

static int openProfileLock(const std::string& dataRoot) {
  makeDirs(dataRoot);
  ensureNoSymlink(dataRoot);
  std::string lockPath = dataRoot + "/.futari-profile.lock";
  int fd = open(lockPath.c_str(), O_RDWR | O_CREAT | O_NOFOLLOW, 0600);
  need(fd >= 0, "Cannot open profile lock");
  struct stat lockStat;
  if (fstat(fd, &lockStat) != 0 || !S_ISREG(lockStat.st_mode)) {
    close(fd);
    throw std::runtime_error("Profile lock is not a regular file");
  }
  if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
    close(fd);
    throw std::runtime_error("Profile is busy; close the game first");
  }
  return fd;
}
class Lock {
 public:
  explicit Lock(const std::string& root) : fd_(openProfileLock(root)) {}
  ~Lock() { if (fd_ >= 0) close(fd_); }
  Lock(const Lock&) = delete;
  Lock& operator=(const Lock&) = delete;
 private: int fd_;
};

static void makeDirs(const std::string& path) {
  ensureNoSymlink(path);
  struct stat info;
  if (lstat(path.c_str(), &info) == 0) {
    need(S_ISDIR(info.st_mode), "Expected directory: " + path);
    return;
  }
  need(errno == ENOENT, "Cannot inspect directory: " + path);
  std::string parent = path.substr(0, path.find_last_of('/'));
  if (parent.empty()) parent = "/";
  if (parent != "/") makeDirs(parent);
  need(mkdir(path.c_str(), 0700) == 0 || errno == EEXIST, "Cannot create directory: " + path);
  ensureNoSymlink(path);
}

static void syncDirectory(const std::string& path) {
  ensureNoSymlink(path);
  int fd = open(path.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
  need(fd >= 0, "Cannot open directory for durable sync: " + path);
  int rc = fsync(fd);
  close(fd);
  need(rc == 0, "Cannot sync directory: " + path);
}

static void syncDirectoryTree(const std::string& path) {
  ensureNoSymlink(path);
  std::unique_ptr<DIR, decltype(&closedir)> dir(opendir(path.c_str()), closedir);
  need(dir != nullptr, "Cannot inspect staged directory: " + path);
  while (dirent* item = nextDirectoryEntry(dir.get(), path)) {
    std::string leaf = item->d_name;
    if (leaf == "." || leaf == "..") continue;
    std::string child = path + "/" + leaf;
    struct stat info;
    need(lstat(child.c_str(), &info) == 0 && !S_ISLNK(info.st_mode),
         "Unsafe staged path: " + child);
    if (S_ISDIR(info.st_mode)) syncDirectoryTree(child);
    else need(S_ISREG(info.st_mode), "Special file in staged directory: " + child);
  }
  syncDirectory(path);
}

static FileIdentity identityOf(const std::string& path, bool directory) {
  ensureNoSymlink(path);
  struct stat info;
  need(lstat(path.c_str(), &info) == 0 && (directory ? S_ISDIR(info.st_mode) : S_ISREG(info.st_mode)),
       "Missing or wrong-type transaction path: " + path);
  return {static_cast<uint64_t>(info.st_dev), static_cast<uint64_t>(info.st_ino)};
}

static bool identityMatches(const std::string& path, uint64_t device, uint64_t inode, bool directory) {
  struct stat info;
  if (lstat(path.c_str(), &info) != 0) {
    need(errno == ENOENT, "Cannot inspect transaction path: " + path);
    return false;
  }
  need(!S_ISLNK(info.st_mode) && (directory ? S_ISDIR(info.st_mode) : S_ISREG(info.st_mode)),
       "Transaction path has an unexpected type: " + path);
  return static_cast<uint64_t>(info.st_dev) == device && static_cast<uint64_t>(info.st_ino) == inode;
}

static bool validUnsignedInteger(id value, uint64_t* result) {
  if (![value isKindOfClass:NSNumber.class] ||
      CFGetTypeID((__bridge CFTypeRef)value) == CFBooleanGetTypeID()) return false;
  NSNumber* number = value;
  const char* type = number.objCType;
  bool integer = strcmp(type, @encode(int)) == 0 || strcmp(type, @encode(long)) == 0 ||
      strcmp(type, @encode(long long)) == 0 || strcmp(type, @encode(unsigned int)) == 0 ||
      strcmp(type, @encode(unsigned long)) == 0 || strcmp(type, @encode(unsigned long long)) == 0;
  if (!integer || number.longLongValue < 0) return false;
  uint64_t parsed = number.unsignedLongLongValue;
  if (result) *result = parsed;
  return true;
}

static FileMap readReplayPackage(const std::string& dataRoot, const std::string& package) {
  validatePackageName(package);
  const std::string title = dataRoot + "/" + kProfile + "/" + kTitle;
  const std::string root = title + "/00000001/" + package;
  ensureNoSymlink(root);
  FileMap files;
  uint64_t total = 0;
  std::function<void(const std::string&, const std::string&)> walk = [&](const std::string& dir, const std::string& prefix) {
    ensureNoSymlink(dir);
    std::unique_ptr<DIR, decltype(&closedir)> stream(opendir(dir.c_str()), closedir);
    need(stream != nullptr, "Cannot read replay package: " + dir);
    while (dirent* item = nextDirectoryEntry(stream.get(), dir)) {
      std::string leaf = item->d_name;
      if (leaf == "." || leaf == "..") continue;
      std::string path = dir + "/" + leaf;
      struct stat info;
      need(lstat(path.c_str(), &info) == 0, "Cannot inspect package member: " + path);
      need(!S_ISLNK(info.st_mode), "Symlink in replay package");
      if (S_ISDIR(info.st_mode)) walk(path, prefix + leaf + "/");
      else {
        need(S_ISREG(info.st_mode), "Special file in replay package");
        std::string name = prefix + leaf;
        need(validMember(name), "Unsafe package path");
        Bytes bytes = readRegular(path, kPayloadLimit);
        total += bytes.size();
        need(files.size() < kMaxFiles && total <= kPayloadLimit, "Replay package exceeds limits");
        files.emplace("payload/" + name, std::move(bytes));
      }
    }
  };
  walk(root, "");
  bool hasReplay = std::any_of(files.begin(), files.end(), [](const auto& item) {
    return item.first.rfind("payload/", 0) == 0 && replayPayload(item.first) && !item.second.empty();
  });
  // Ordinary systemdata/save packages are not replay archives and are ignored.
  if (!hasReplay) return {};
  Bytes header = readRegular(title + "/Headers/00000001/" + package + ".header", 0x14c);
  validateHeader(header, package);
  files.emplace("header.bin", std::move(header));
  need(std::any_of(files.begin(), files.end(), [](const auto& item) {
    return item.first.rfind("payload/", 0) == 0 && replayPayload(item.first) && !item.second.empty();
  }), "No non-empty replaydata*.bin payload; package is not a confirmed replay");
  return files;
}

static NSDictionary* makeManifest(const std::string& package, const std::string& content,
                                  const std::string& build, const FileMap& files) {
  NSMutableDictionary* roster = [NSMutableDictionary dictionary];
  for (const auto& item : files)
    roster[ns(item.first)] = @{@"bytes": @(item.second.size()), @"sha256": ns(sha256(item.second))};
  return @{@"format": ns(kFormat), @"title": ns(kTitle), @"profile": ns(kProfile),
           @"package": ns(package), @"content": ns(content), @"build_sha256": ns(build),
           @"verification": @"package integrity only; playback not certified", @"files": roster};
}

static void archiveCheck(struct archive* a, int status, const std::string& message) {
  need(status == ARCHIVE_OK, message);
}
static void writeArchive(int fd, const FileMap& files) {
  struct archive* a = archive_write_new();
  need(a != nullptr, "Cannot allocate archive writer");
  try {
    archiveCheck(a, archive_write_add_filter_none(a), "Cannot configure ZIP filter");
    archiveCheck(a, archive_write_set_format_zip(a), "Cannot configure ZIP format");
    int archiveFD = dup(fd);
    need(archiveFD >= 0, "Cannot duplicate export staging descriptor");
    int openStatus = archive_write_open_fd(a, archiveFD);
    if (openStatus != ARCHIVE_OK) close(archiveFD);
    archiveCheck(a, openStatus, "Cannot create replay archive");
    for (const auto& item : files) {
      struct archive_entry* entry = archive_entry_new();
      need(entry != nullptr, "Cannot allocate ZIP entry");
      archive_entry_set_pathname_utf8(entry, item.first.c_str());
      archive_entry_set_filetype(entry, AE_IFREG);
      archive_entry_set_perm(entry, 0600);
      archive_entry_set_size(entry, static_cast<la_int64_t>(item.second.size()));
      try {
        archiveCheck(a, archive_write_header(a, entry), "Cannot write ZIP entry header");
        size_t offset = 0;
        while (offset < item.second.size()) {
          la_ssize_t n = archive_write_data(a, item.second.data() + offset, item.second.size() - offset);
          need(n > 0, "Cannot write ZIP entry payload");
          offset += static_cast<size_t>(n);
        }
      } catch (...) { archive_entry_free(entry); throw; }
      archive_entry_free(entry);
    }
    archiveCheck(a, archive_write_close(a), "Cannot finish replay archive");
  } catch (...) { archive_write_free(a); throw; }
  need(archive_write_free(a) == ARCHIVE_OK, "Cannot release ZIP writer");
}

static FileMap readArchive(const std::string& path) {
  ensureNoSymlink(path);
  int archiveFD = open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
  need(archiveFD >= 0, "Cannot open selected replay archive");
  struct stat archiveStat;
  if (fstat(archiveFD, &archiveStat) != 0 || !S_ISREG(archiveStat.st_mode) ||
      archiveStat.st_size < 0 || static_cast<uint64_t>(archiveStat.st_size) > kArchiveLimit) {
    close(archiveFD);
    throw std::runtime_error("Replay archive is not a bounded regular file");
  }
  struct archive* a = archive_read_new();
  if (!a) { close(archiveFD); throw std::runtime_error("Cannot allocate archive reader"); }
  FileMap files;
  std::set<std::string> foldedNames;
  uint64_t total = 0;
  size_t count = 0;
  try {
    archiveCheck(a, archive_read_support_filter_none(a), "Cannot configure archive filters");
    archiveCheck(a, archive_read_support_format_zip(a), "Cannot configure ZIP reader");
    archiveCheck(a, archive_read_open_fd(a, archiveFD, 10240), "Not a readable ZIP archive");
    for (;;) {
      struct archive_entry* entry = nullptr;
      int rc = archive_read_next_header(a, &entry);
      if (rc == ARCHIVE_EOF) break;
      archiveCheck(a, rc, "Invalid ZIP entry");
      need(++count <= kMaxFiles + 2, "Archive has too many entries");
      need(entry != nullptr && archive_entry_is_encrypted(entry) == 0,
           "Encrypted or unclassified ZIP member rejected");
      need(archive_entry_filetype(entry) == AE_IFREG, "ZIP directories, links, and special files rejected");
      const char* memberRaw = archive_entry_pathname_utf8(entry);
      need(memberRaw != nullptr, "ZIP member name is not UTF-8");
      std::string member(memberRaw);
      need(validMember(member), "Unsafe archive member name");
      NSString* canonical = ns(member).precomposedStringWithCanonicalMapping;
      std::string folded = str(canonical.lowercaseString.decomposedStringWithCanonicalMapping);
      need(foldedNames.insert(folded).second, "Duplicate or case-colliding ZIP member");
      la_int64_t expected = archive_entry_size(entry);
      need(expected >= 0 && static_cast<uint64_t>(expected) <= kPayloadLimit,
           "Archive entry exceeds size limit");
      total += static_cast<uint64_t>(expected);
      need(total <= kPayloadLimit, "Expanded archive exceeds size limit");
      Bytes bytes(static_cast<size_t>(expected));
      size_t offset = 0;
      while (offset < bytes.size()) {
        la_ssize_t n = archive_read_data(a, bytes.data() + offset, bytes.size() - offset);
        need(n > 0, "Short or corrupt ZIP payload");
        offset += static_cast<size_t>(n);
      }
      uint8_t extra;
      la_ssize_t trailing = archive_read_data(a, &extra, 1);
      need(trailing == 0, "ZIP payload exceeds declared size");
      files.emplace(std::move(member), std::move(bytes));
    }
    int encrypted = archive_read_has_encrypted_entries(a);
    need(encrypted == 0, "ZIP encryption state is unsupported or unknown");
    archiveCheck(a, archive_read_close(a), "Cannot close ZIP archive cleanly");
  } catch (...) { archive_read_free(a); close(archiveFD); throw; }
  int freeStatus = archive_read_free(a);
  close(archiveFD);
  need(freeStatus == ARCHIVE_OK, "Cannot release ZIP reader");
  need(!files.empty(), "Empty replay archive");
  return files;
}

static std::string validateArchive(FileMap& files, const std::string& content,
                                   const std::string& build) {
  auto manifest = files.find("manifest.json");
  need(manifest != files.end() && manifest->second.size() <= 65536, "Missing or oversized manifest.json");
  NSDictionary* m = parseJSON(manifest->second);
  need(jsonString(m, @"format") == kFormat && jsonString(m, @"title") == kTitle &&
       jsonString(m, @"profile") == kProfile, "Wrong replay format, title, or profile");
  need(jsonString(m, @"content") == content && jsonString(m, @"build_sha256") == build,
       "Replay is for another content selection or build");
  std::string package = jsonString(m, @"package");
  validatePackageName(package);
  NSDictionary* roster = m[@"files"];
  need([roster isKindOfClass:NSDictionary.class] && roster[@"manifest.json"] == nil &&
       roster[@"header.bin"] != nil && roster.count == files.size() - 1,
       "Replay manifest/file roster mismatch");
  bool hasReplay = false;
  for (NSString* key in roster) {
    std::string name = str(key);
    need(name == "header.bin" || name.rfind("payload/", 0) == 0, "Unexpected manifest member");
    auto found = files.find(name);
    need(found != files.end(), "Manifest member missing from archive");
    NSDictionary* spec = roster[key];
    need([spec isKindOfClass:NSDictionary.class], "Invalid manifest file entry");
    id bytes = spec[@"bytes"];
    need(validJSONSize(bytes, found->second.size()),
         "Replay file size mismatch");
    need(jsonString(spec, @"sha256") == sha256(found->second), "Replay file checksum mismatch");
    if (name.rfind("payload/", 0) == 0 && replayPayload(name) && !found->second.empty()) hasReplay = true;
  }
  need(hasReplay, "No confirmed replay payload");
  validateHeader(files.at("header.bin"), package);
  files.erase("manifest.json");
  return package;
}

struct PendingImport {
  std::string package, content, build, nonce, archiveHash, bytes;
  struct RosterEntry { uint64_t bytes, device, inode; std::string hash; };
  std::map<std::string, RosterEntry> roster;
  uint64_t stageDevice = 0, stageInode = 0, packageDevice = 0, packageInode = 0;
  uint64_t headerDevice = 0, headerInode = 0, journalDevice = 0, journalInode = 0;
};

// The fixed journal is an immutable, forward-only intent. flock serializes the
// helper and launcher when they cooperate; it cannot stop arbitrary same-UID
// namespace races. File descriptors bind regular-file hashes to their recorded
// inodes, while all observed ambiguity is refused. fsync calls request local
// filesystem durability; these synthetic tests do not simulate power loss.

static std::string stagePath(const std::string& dataRoot, const PendingImport& tx) {
  return dataRoot + "/.futari-replay-import-staging/.replay-import-" + tx.nonce;
}
static std::string packagePath(const std::string& dataRoot, const PendingImport& tx) {
  return dataRoot + "/" + kProfile + "/" + kTitle + "/00000001/" + tx.package;
}
static std::string stagedPackagePath(const std::string& dataRoot, const PendingImport& tx) {
  return stagePath(dataRoot, tx) + "/package";
}
static std::string stagedHeaderPath(const std::string& dataRoot, const PendingImport& tx) {
  return stagePath(dataRoot, tx) + "/header";
}
static std::string publicHeaderPath(const std::string& dataRoot, const PendingImport& tx) {
  return dataRoot + "/" + kProfile + "/" + kTitle + "/Headers/00000001/" + tx.package + ".header";
}

static NSDictionary* journalObject(const PendingImport& tx) {
  NSMutableDictionary* roster = [NSMutableDictionary dictionary];
  for (const auto& entry : tx.roster)
    roster[ns(entry.first)] = @{ @"bytes": @(entry.second.bytes), @"sha256": ns(entry.second.hash),
                                 @"device": @(entry.second.device), @"inode": @(entry.second.inode) };
  return @{ @"schema": ns(kJournalSchema), @"title": ns(kTitle), @"profile": ns(kProfile),
            @"content": ns(tx.content), @"build_sha256": ns(tx.build), @"package": ns(tx.package),
            @"archive_sha256": ns(tx.archiveHash), @"nonce": ns(tx.nonce),
            @"stage_device": @(tx.stageDevice), @"stage_inode": @(tx.stageInode),
            @"package_device": @(tx.packageDevice), @"package_inode": @(tx.packageInode),
            @"header_device": @(tx.headerDevice), @"header_inode": @(tx.headerInode),
            @"files": roster };
}

static PendingImport parseJournal(const std::string& dataRoot) {
  const std::string path = dataRoot + "/" + kPendingJournal;
  FileIdentity ji;
  Bytes raw = readRegular(path, 256 * 1024, nullptr, &ji);
  PendingImport tx;
  tx.bytes.assign(reinterpret_cast<const char*>(raw.data()), raw.size());
  tx.journalDevice = ji.device; tx.journalInode = ji.inode;
  NSDictionary* d = parseJSON(raw);
  static const std::set<std::string> expected = {
      "schema", "title", "profile", "content", "build_sha256", "package", "archive_sha256",
      "nonce", "stage_device", "stage_inode", "package_device", "package_inode",
      "header_device", "header_inode", "files"};
  NSMutableSet* actual = [NSMutableSet set];
  for (id key in d) {
    need([key isKindOfClass:NSString.class], "Invalid pending journal key");
    [actual addObject:key];
  }
  need(actual.count == expected.size(), "Pending journal has missing or extra fields");
  for (const auto& key : expected) need([actual containsObject:ns(key)], "Pending journal has missing fields");
  need(jsonString(d, @"schema") == kJournalSchema && jsonString(d, @"title") == kTitle &&
       jsonString(d, @"profile") == kProfile, "Unsupported pending replay journal");
  tx.content = jsonString(d, @"content");
  tx.build = jsonString(d, @"build_sha256");
  tx.package = jsonString(d, @"package");
  tx.archiveHash = jsonString(d, @"archive_sha256");
  tx.nonce = jsonString(d, @"nonce");
  validatePackageName(tx.package);
  need(validContent(tx.content) && validBuild(tx.build) && validBuild(tx.archiveHash),
       "Invalid pending journal identity");
  need(tx.nonce.size() == 32 && std::all_of(tx.nonce.begin(), tx.nonce.end(), [](unsigned char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
  }), "Invalid pending journal stage identity");
  auto readUInt = [&](NSString* key) {
    uint64_t value = 0;
    need(validUnsignedInteger(d[key], &value), "Invalid integer in pending journal: " + str(key));
    return value;
  };
  tx.stageDevice = readUInt(@"stage_device"); tx.stageInode = readUInt(@"stage_inode");
  tx.packageDevice = readUInt(@"package_device"); tx.packageInode = readUInt(@"package_inode");
  tx.headerDevice = readUInt(@"header_device"); tx.headerInode = readUInt(@"header_inode");
  for (uint64_t value : {tx.stageInode, tx.packageInode, tx.headerInode})
    need(value != 0, "Invalid zero inode in pending journal");

  NSDictionary* roster = d[@"files"];
  need([roster isKindOfClass:NSDictionary.class] && roster.count >= 2 && roster.count <= kMaxFiles + 1,
       "Invalid pending journal file roster");
  uint64_t total = 0;
  std::set<std::string> folded;
  bool hasReplay = false;
  for (id key in roster) {
    need([key isKindOfClass:NSString.class], "Invalid pending journal roster key");
    std::string name = str(key);
    need(name == "header.bin" || name.rfind("payload/", 0) == 0, "Unexpected pending journal member");
    if (name.rfind("payload/", 0) == 0) need(validMember(name.substr(8)), "Unsafe pending journal member");
    NSString* normalized = ns(name).precomposedStringWithCanonicalMapping;
    std::string foldedName = str(normalized.lowercaseString.decomposedStringWithCanonicalMapping);
    need(folded.insert(foldedName).second, "Colliding pending journal member");
    NSDictionary* spec = roster[key];
    need([spec isKindOfClass:NSDictionary.class] && spec.count == 4 &&
         spec[@"bytes"] != nil && spec[@"sha256"] != nil && spec[@"device"] != nil && spec[@"inode"] != nil,
         "Invalid pending journal file entry");
    uint64_t size = 0, device = 0, inode = 0;
    need(validUnsignedInteger(spec[@"bytes"], &size) && size <= kPayloadLimit &&
         validUnsignedInteger(spec[@"device"], &device) && validUnsignedInteger(spec[@"inode"], &inode) &&
         inode != 0,
         "Invalid pending journal file size");
    std::string hash = jsonString(spec, @"sha256");
    need(validBuild(hash), "Invalid pending journal file checksum");
    total += size;
    need(total <= kPayloadLimit + 0x14c, "Pending journal payload exceeds limit");
    if (name == "header.bin") need(size == 0x148 || size == 0x14c, "Invalid pending header size");
    if (name.rfind("payload/", 0) == 0 && replayPayload(name) && size > 0) hasReplay = true;
    tx.roster.emplace(name, PendingImport::RosterEntry{size, device, inode, hash});
  }
  need(tx.roster.count("header.bin") && hasReplay, "Pending journal lacks a replay payload or header");
  need(tx.roster.at("header.bin").device == tx.headerDevice &&
       tx.roster.at("header.bin").inode == tx.headerInode,
       "Pending journal header identity disagrees with its roster");
  need(encodeJSON(d) == raw, "Pending journal is non-canonical or has duplicate fields");
  return tx;
}

static bool pathExistsNoLink(const std::string& path) {
  struct stat info;
  if (lstat(path.c_str(), &info) == 0) {
    need(!S_ISLNK(info.st_mode), "Symlink transaction path rejected: " + path);
    return true;
  }
  need(errno == ENOENT, "Cannot inspect transaction path: " + path);
  return false;
}

static void verifyJournalUnchanged(const std::string& dataRoot, const PendingImport& tx) {
  const std::string path = dataRoot + "/" + kPendingJournal;
  FileIdentity expected{tx.journalDevice, tx.journalInode};
  Bytes bytes = readRegular(path, 256 * 1024, &expected);
  need(std::string(reinterpret_cast<const char*>(bytes.data()), bytes.size()) == tx.bytes,
       "Pending replay journal changed during recovery");
}

static Bytes verifyFileSpec(const std::string& path, const std::string& name, const PendingImport& tx) {
  auto spec = tx.roster.find(name);
  need(spec != tx.roster.end(), "Unlisted transaction member: " + name);
  FileIdentity expected{spec->second.device, spec->second.inode};
  Bytes bytes = readRegular(path, kPayloadLimit, &expected);
  need(bytes.size() == spec->second.bytes && sha256(bytes) == spec->second.hash,
       "Transaction member changed: " + name);
  return bytes;
}

static size_t verifyPayloadTree(const std::string& root, const std::string& prefix, const PendingImport& tx) {
  ensureNoSymlink(root);
  std::unique_ptr<DIR, decltype(&closedir)> dir(opendir(root.c_str()), closedir);
  need(dir != nullptr, "Cannot inspect replay transaction package");
  size_t files = 0;
  while (dirent* item = nextDirectoryEntry(dir.get(), root)) {
    std::string leaf = item->d_name;
    if (leaf == "." || leaf == "..") continue;
    std::string child = root + "/" + leaf;
    std::string rel = prefix.empty() ? leaf : prefix + "/" + leaf;
    struct stat info;
    need(lstat(child.c_str(), &info) == 0 && !S_ISLNK(info.st_mode), "Unsafe replay transaction member");
    if (S_ISDIR(info.st_mode)) {
      size_t nested = verifyPayloadTree(child, rel, tx);
      need(nested > 0, "Unexpected empty directory in replay transaction package");
      files += nested;
    }
    else {
      need(S_ISREG(info.st_mode) && validMember(rel), "Invalid replay transaction file");
      verifyFileSpec(child, "payload/" + rel, tx);
      ++files;
    }
  }
  return files;
}

static void verifyExactPayload(const std::string& root, const PendingImport& tx) {
  need(identityMatches(root, tx.packageDevice, tx.packageInode, true),
       "Replay package ownership identity changed");
  size_t actual = verifyPayloadTree(root, "", tx);
  size_t found = 0;
  for (const auto& entry : tx.roster) if (entry.first.rfind("payload/", 0) == 0) ++found;
  need(actual == found, "Replay package roster is incomplete");
}

static void verifyHeaderSpec(const std::string& path, const PendingImport& tx) {
  need(identityMatches(path, tx.headerDevice, tx.headerInode, false),
       "Replay header ownership identity changed");
  Bytes header = verifyFileSpec(path, "header.bin", tx);
  validateHeader(header, tx.package);
}

static std::vector<std::string> stageEntries(const std::string& stage) {
  std::unique_ptr<DIR, decltype(&closedir)> dir(opendir(stage.c_str()), closedir);
  need(dir != nullptr, "Cannot inspect transaction staging directory");
  std::vector<std::string> entries;
  while (dirent* item = nextDirectoryEntry(dir.get(), stage)) {
    std::string leaf = item->d_name;
    if (leaf == "." || leaf == "..") continue;
    entries.push_back(leaf);
  }
  std::sort(entries.begin(), entries.end());
  return entries;
}

static void cleanupStage(const std::string& dataRoot, const PendingImport& tx) {
  const std::string stage = stagePath(dataRoot, tx);
  if (!pathExistsNoLink(stage)) return;
  need(identityMatches(stage, tx.stageDevice, tx.stageInode, true), "Transaction staging directory ownership changed");
  ScopedFD stageHandle(open(stage.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW));
  int stageFD = stageHandle.get();
  need(stageFD >= 0, "Cannot open owned transaction staging directory");
  struct stat openedStage;
  if (fstat(stageFD, &openedStage) != 0 || static_cast<uint64_t>(openedStage.st_dev) != tx.stageDevice ||
      static_cast<uint64_t>(openedStage.st_ino) != tx.stageInode) {
    throw std::runtime_error("Transaction staging directory changed during cleanup");
  }
  std::vector<std::string> entries = stageEntries(stage);
  for (const auto& leaf : entries) need(leaf == "header", "Unexpected private transaction staging entry");
  if (std::find(entries.begin(), entries.end(), "header") != entries.end()) {
    struct stat headerInfo;
    need(fstatat(stageFD, "header", &headerInfo, AT_SYMLINK_NOFOLLOW) == 0 &&
         S_ISREG(headerInfo.st_mode) && static_cast<uint64_t>(headerInfo.st_dev) == tx.headerDevice &&
         static_cast<uint64_t>(headerInfo.st_ino) == tx.headerInode,
         "Private staged header ownership changed during cleanup");
    verifyHeaderSpec(stage + "/header", tx);
    need(unlinkat(stageFD, "header", 0) == 0, "Cannot remove verified private staged header");
  }
  need(fsync(stageFD) == 0, "Cannot sync private replay staging cleanup");
  std::string parent = stage.substr(0, stage.find_last_of('/'));
  std::string leaf = stage.substr(stage.find_last_of('/') + 1);
  ScopedFD parentHandle(open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW));
  int parentFD = parentHandle.get();
  need(parentFD >= 0, "Cannot open private replay staging parent");
  struct stat current;
  need(fstatat(parentFD, leaf.c_str(), &current, AT_SYMLINK_NOFOLLOW) == 0 && S_ISDIR(current.st_mode) &&
       static_cast<uint64_t>(current.st_dev) == tx.stageDevice &&
       static_cast<uint64_t>(current.st_ino) == tx.stageInode,
       "Private staging directory ownership changed before removal");
  need(unlinkat(parentFD, leaf.c_str(), AT_REMOVEDIR) == 0, "Cannot remove empty owned transaction staging directory");
  need(fsync(parentFD) == 0, "Cannot sync staging parent after cleanup");
}

static void faultExit(const char* boundary) {
#if defined(FUTARI_REPLAY_TEST_FAULTS)
  const char* requested = getenv("FUTARI_REPLAY_TEST_FAULT");
  if (requested != nullptr && strcmp(requested, boundary) == 0) _exit(86);
#else
  (void)boundary;
#endif
}

static bool recoverPendingImport(const std::string& dataRoot, const std::string& content,
                                const std::string& build) {
  const std::string journalPath = dataRoot + "/" + kPendingJournal;
  if (!pathExistsNoLink(journalPath)) {
    std::cout << "No pending replay import\n";
    return false;
  }
  PendingImport tx = parseJournal(dataRoot);
  need(tx.content == content && tx.build == build, "Pending replay import is for another content selection or build");
  const std::string stage = stagePath(dataRoot, tx), stagedPackage = stagedPackagePath(dataRoot, tx);
  const std::string stagedHeader = stagedHeaderPath(dataRoot, tx), target = packagePath(dataRoot, tx);
  const std::string header = publicHeaderPath(dataRoot, tx);
  ensureNoSymlink(stage); ensureNoSymlink(stagedPackage); ensureNoSymlink(stagedHeader);
  ensureNoSymlink(target); ensureNoSymlink(header);

  bool hasStage = pathExistsNoLink(stage);
  if (hasStage) need(identityMatches(stage, tx.stageDevice, tx.stageInode, true),
                     "Transaction staging directory ownership changed");
  bool hasStagedPackage = pathExistsNoLink(stagedPackage);
  bool hasStagedHeader = pathExistsNoLink(stagedHeader);
  bool hasTarget = pathExistsNoLink(target);
  bool hasHeader = pathExistsNoLink(header);
  if (hasStagedPackage) {
    need(hasStage && identityMatches(stagedPackage, tx.packageDevice, tx.packageInode, true),
         "Staged replay package ownership changed");
    verifyExactPayload(stagedPackage, tx);
  }
  if (hasTarget) {
    need(identityMatches(target, tx.packageDevice, tx.packageInode, true),
         "Published replay package conflicts with pending journal");
    verifyExactPayload(target, tx);
  }
  if (hasStagedHeader) {
    need(hasStage, "Staged header has no owned staging directory");
    verifyHeaderSpec(stagedHeader, tx);
  }
  if (hasHeader) verifyHeaderSpec(header, tx);
  need(!hasHeader || hasTarget, "Published replay header exists without its package");
  need(hasStagedPackage != hasTarget, "Replay package transaction state is ambiguous");
  need(hasStagedHeader || hasHeader, "No owned replay header remains for recovery");

  // A prior process may have published the journal but died before syncing its
  // rename parents. Make the intent and its remaining stage durable before any
  // recovery publication. A package found at its exact recorded inode is also
  // synced before recovery can publish the separate header.
  syncDirectory(dataRoot);
  if (hasStage) syncDirectory(stage);
  if (hasTarget) syncDirectory(target.substr(0, target.find_last_of('/')));

  if (hasStage) {
    std::vector<std::string> entries = stageEntries(stage);
    for (const auto& leaf : entries)
      need(leaf == "package" || leaf == "header", "Unexpected private transaction staging entry");
  }
  if (!hasTarget) {
    need(hasStage && hasStagedPackage && !hasHeader, "Cannot safely forward-publish replay package");
    verifyJournalUnchanged(dataRoot, tx);
    int rc = renameatx_np(AT_FDCWD, stagedPackage.c_str(), AT_FDCWD, target.c_str(), RENAME_EXCL);
    if (rc != 0) throw std::runtime_error(errno == EEXIST ?
        "Replay package destination conflicts; existing save preserved" : "Cannot publish replay package");
    hasTarget = true;
    need(identityMatches(target, tx.packageDevice, tx.packageInode, true),
         "Published replay package does not match its staged identity");
    faultExit("package_renamed");
    syncDirectory(stage);
    syncDirectory(target.substr(0, target.find_last_of('/')));
    faultExit("package_published");
  }
  if (!hasHeader) {
    need(hasStagedHeader, "Cannot safely forward-publish replay header");
    verifyJournalUnchanged(dataRoot, tx);
    if (link(stagedHeader.c_str(), header.c_str()) != 0)
      throw std::runtime_error(errno == EEXIST ?
          "Replay header destination conflicts; existing save preserved" : "Cannot publish replay header");
    hasHeader = true;
    verifyHeaderSpec(header, tx);
    faultExit("header_linked");
    syncDirectory(header.substr(0, header.find_last_of('/')));
    faultExit("header_published");
  }
  verifyExactPayload(target, tx);
  verifyHeaderSpec(header, tx);
  // A prior process may have died after rename/link but before its directory
  // fsync. Re-sync the exact published parents before retiring the intent.
  syncDirectory(target.substr(0, target.find_last_of('/')));
  syncDirectory(header.substr(0, header.find_last_of('/')));
  cleanupStage(dataRoot, tx);
  faultExit("stage_cleanup");
  verifyJournalUnchanged(dataRoot, tx);
  ScopedFD dataFD(open(dataRoot.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW));
  need(dataFD.get() >= 0, "Cannot open profile root to retire replay journal");
  struct stat journalInfo;
  need(fstatat(dataFD.get(), kPendingJournal, &journalInfo, AT_SYMLINK_NOFOLLOW) == 0 &&
       S_ISREG(journalInfo.st_mode) && static_cast<uint64_t>(journalInfo.st_dev) == tx.journalDevice &&
       static_cast<uint64_t>(journalInfo.st_ino) == tx.journalInode,
       "Pending replay journal ownership changed before retirement");
  need(unlinkat(dataFD.get(), kPendingJournal, 0) == 0, "Cannot remove completed replay journal");
  need(fsync(dataFD.get()) == 0, "Cannot sync profile root after journal retirement");
  faultExit("completed_cleanup");
  std::cout << tx.package << ": replay import recovered\n";
  return true;
}

static void installJournal(const std::string& dataRoot, const std::string& stage,
                           const PendingImport& tx) {
  const std::string path = dataRoot + "/" + kPendingJournal;
  const std::string stagedPath = stage + "/journal";
  Bytes bytes = encodeJSON(journalObject(tx));
  need(bytes.size() <= 256 * 1024, "Pending replay journal exceeds the recovery size limit");
  writeExclusive(stagedPath, bytes);
  syncDirectory(stage);
  faultExit("journal_staged");
  int rc = renameatx_np(AT_FDCWD, stagedPath.c_str(), AT_FDCWD, path.c_str(), RENAME_EXCL);
  need(rc == 0, errno == EEXIST ?
       "A pending replay journal already exists" : "Cannot publish pending replay journal");
  faultExit("journal_published");
  syncDirectory(stage);
  syncDirectory(dataRoot);
  faultExit("prepared");
}

static void makePendingIntent(const std::string& dataRoot,
                              const std::string& content, const std::string& build,
                              const std::string& package, const FileMap& files, const std::string& archiveHash,
                              const std::string& stage) {
  PendingImport tx;
  tx.package = package; tx.content = content; tx.build = build;
  tx.archiveHash = archiveHash;
  std::string leaf = stage.substr(stage.find_last_of('/') + 1);
  const std::string prefix = ".replay-import-";
  need(leaf.rfind(prefix, 0) == 0, "Invalid private transaction staging name");
  tx.nonce = leaf.substr(prefix.size());
  FileIdentity si = identityOf(stage, true), pi = identityOf(stage + "/package", true),
               hi = identityOf(stage + "/header", false);
  tx.stageDevice = si.device; tx.stageInode = si.inode;
  tx.packageDevice = pi.device; tx.packageInode = pi.inode;
  tx.headerDevice = hi.device; tx.headerInode = hi.inode;
  for (const auto& item : files) {
    std::string path = item.first == "header.bin" ? stage + "/header" :
        stage + "/package/" + item.first.substr(8);
    FileIdentity fi = identityOf(path, false);
    tx.roster.emplace(item.first, PendingImport::RosterEntry{
        static_cast<uint64_t>(item.second.size()), fi.device, fi.inode, sha256(item.second)});
  }
  verifyExactPayload(stage + "/package", tx);
  verifyHeaderSpec(stage + "/header", tx);
  installJournal(dataRoot, stage, tx);
}

static void exportReplays(const std::string& dataRoot, const std::string& destination,
                          const std::string& content, const std::string& build) {
  need(validContent(content) && validBuild(build), "Invalid content or package build identity");
  Lock lock(dataRoot);
  recoverPendingImport(dataRoot, content, build);
  makeDirs(destination);
  ensureNoSymlink(destination);
  std::string root = dataRoot + "/" + kProfile + "/" + kTitle + "/00000001";
  ensureNoSymlink(root);
  DIR* dir = opendir(root.c_str());
  if (!dir) {
    need(errno == ENOENT, "Cannot open native save directory");
    std::cout << "No replay payloads found\n";
    return;
  }
  std::unique_ptr<DIR, decltype(&closedir)> directory(dir, closedir);
  size_t replayPackages = 0, exportedPackages = 0, failedPackages = 0;
  while (dirent* item = nextDirectoryEntry(directory.get(), root)) {
    std::string package = item->d_name;
    if (package == "." || package == ".." || package[0] == '.') continue;
    std::string packagePath = root + "/" + package;
    struct stat info;
    need(lstat(packagePath.c_str(), &info) == 0 && !S_ISLNK(info.st_mode), "Unsafe save package path");
    if (!S_ISDIR(info.st_mode)) continue;
    try {
      FileMap files = readReplayPackage(dataRoot, package);
      if (files.empty()) continue;
      ++replayPackages;
      Bytes manifest = encodeJSON(makeManifest(package, content, build, files));
      std::string archiveName = sha256(manifest) + kExtension;
      std::string finalPath = destination + "/" + archiveName;
      files.emplace("manifest.json", std::move(manifest));
      struct stat existing;
      if (lstat(finalPath.c_str(), &existing) == 0) {
        need(!S_ISLNK(existing.st_mode), "Existing export is a symlink; preserving it");
        need(readArchive(finalPath) == files, "Existing export differs; preserving it");
        std::cout << package << ": already exported " << finalPath << "\n";
        ++exportedPackages;
        continue;
      }
      need(errno == ENOENT, "Cannot inspect export destination");
      std::string pattern = destination + "/.replay-export-XXXXXX";
      std::vector<char> tempName(pattern.begin(), pattern.end()); tempName.push_back('\0');
      int tempFD = mkstemp(tempName.data());
      need(tempFD >= 0, "Cannot create private export staging file");
      std::string tmp(tempName.data());
      try {
        need(fchmod(tempFD, 0600) == 0, "Cannot protect staged replay archive");
        writeArchive(tempFD, files);
        need(fsync(tempFD) == 0, "Cannot sync staged replay archive");
        close(tempFD); tempFD = -1;
        if (link(tmp.c_str(), finalPath.c_str()) != 0) {
          if (errno == EEXIST && readArchive(finalPath) == files) {
            unlink(tmp.c_str());
            std::cout << package << ": already exported " << finalPath << "\n";
            ++exportedPackages;
            continue;
          }
          throw std::runtime_error(errno == EEXIST ?
              "Existing export differs; preserving it" : "Cannot publish replay archive");
        }
        std::cout << package << ": exported " << finalPath << "\n";
        ++exportedPackages;
      } catch (...) {
        if (tempFD >= 0) close(tempFD);
        unlink(tmp.c_str());
        throw;
      }
      unlink(tmp.c_str());
    } catch (const std::exception& e) {
      ++failedPackages;
      std::cerr << package << ": not exported: " << e.what() << "\n";
    }
  }
  if (replayPackages == 0 && failedPackages == 0) {
    std::cout << "No replay payloads found\n";
    return;
  }
  std::cout << "Replay export summary: replay_packages=" << replayPackages
            << " exported_or_already_present=" << exportedPackages
            << " failed=" << failedPackages << "\n";
  need(failedPackages == 0, "One or more replay packages were not exported; see per-package errors above");
}

static void importReplay(const std::string& archivePath, const std::string& dataRoot,
                         const std::string& content, const std::string& build) {
  need(validContent(content) && validBuild(build), "Invalid content or package build identity");
  need(archivePath.size() >= sizeof(kExtension) - 1 &&
       archivePath.compare(archivePath.size() - (sizeof(kExtension) - 1), sizeof(kExtension) - 1, kExtension) == 0,
       "Choose one .futari-replay archive");
  Lock lock(dataRoot);
  recoverPendingImport(dataRoot, content, build);
  std::string archiveHash = sha256(readRegular(archivePath, kArchiveLimit));
  FileMap files = readArchive(archivePath);
  need(sha256(readRegular(archivePath, kArchiveLimit)) == archiveHash,
       "Replay archive changed while it was being validated");
  std::string package = validateArchive(files, content, build);
  std::string title = dataRoot + "/" + kProfile + "/" + kTitle;
  std::string target = title + "/00000001/" + package;
  std::string header = title + "/Headers/00000001/" + package + ".header";
  ensureNoSymlink(target); ensureNoSymlink(header);
  struct stat info;
  int targetResult = lstat(target.c_str(), &info);
  bool targetExists = targetResult == 0;
  if (targetResult != 0) need(errno == ENOENT, "Cannot inspect existing replay package");
  int headerResult = lstat(header.c_str(), &info);
  bool headerExists = headerResult == 0;
  if (headerResult != 0) need(errno == ENOENT, "Cannot inspect existing replay header");
  if (targetExists && headerExists) {
    FileMap existing = readReplayPackage(dataRoot, package);
    need(existing == files, "Save-slot conflict; existing data was preserved (no replacement)");
    std::cout << package << ": already present\n";
    return;
  }
  if (targetExists || headerExists) {
    throw std::runtime_error("Save-slot conflict; existing data was preserved (no replacement)");
  }
  makeDirs(title + "/00000001"); makeDirs(title + "/Headers/00000001");
  const std::string stagingRoot = dataRoot + "/.futari-replay-import-staging";
  makeDirs(stagingRoot);
  std::string nonce;
  static const char hex[] = "0123456789abcdef";
  for (int attempt = 0; attempt < 8; ++attempt) {
    uint8_t random[16]; arc4random_buf(random, sizeof(random));
    nonce.clear();
    for (uint8_t byte : random) { nonce.push_back(hex[byte >> 4]); nonce.push_back(hex[byte & 15]); }
    std::string candidate = stagingRoot + "/.replay-import-" + nonce;
    if (mkdir(candidate.c_str(), 0700) == 0) break;
    need(errno == EEXIST && attempt != 7, "Cannot create private import staging directory");
    nonce.clear();
  }
  need(!nonce.empty(), "Cannot allocate private import staging identity");
  std::string stage = stagingRoot + "/.replay-import-" + nonce;
  std::string stagedPackage = stage + "/package";
  need(mkdir(stagedPackage.c_str(), 0700) == 0, "Cannot stage replay package");
  for (const auto& item : files) {
    if (item.first == "header.bin") continue;
    need(item.first.rfind("payload/", 0) == 0, "Unexpected replay member");
    std::string relative = item.first.substr(8), path = stagedPackage;
    size_t pos = 0;
    for (;;) {
      size_t slash = relative.find('/', pos);
      if (slash == std::string::npos) break;
      path += "/" + relative.substr(pos, slash - pos);
      need(mkdir(path.c_str(), 0700) == 0 || errno == EEXIST, "Cannot create staged subdirectory");
      struct stat st; need(lstat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode) && !S_ISLNK(st.st_mode),
                           "Unsafe staged subdirectory");
      pos = slash + 1;
    }
    writeExclusive(path + "/" + relative.substr(pos), item.second);
  }
  std::string stagedHeader = stage + "/header";
  writeExclusive(stagedHeader, files.at("header.bin"));
  syncDirectoryTree(stagedPackage);
  syncDirectory(stage);
  syncDirectory(title + "/00000001");
  syncDirectory(title);
  syncDirectory(dataRoot + "/" + kProfile);
  syncDirectory(dataRoot);
  syncDirectory(title + "/Headers/00000001");
  syncDirectory(title + "/Headers");
  syncDirectory(stagingRoot);
  faultExit("staged_before_journal");
  makePendingIntent(dataRoot, content, build, package, files, archiveHash, stage);
  recoverPendingImport(dataRoot, content, build);
  std::cout << package << ": imported\n";
}
} // namespace futari_replay

int main(int argc, char** argv) {
  @autoreleasepool {
    try {
      const char* version = archive_version_string();
      futari_replay::need(archive_version_number() >= 3000000 && version != nullptr,
                          "System libarchive is unavailable or too old");
      if (argc == 6 && std::string(argv[1]) == "--export") {
        futari_replay::exportReplays(argv[2], argv[3], argv[4], argv[5]);
      } else if (argc == 6 && std::string(argv[1]) == "--import") {
        futari_replay::importReplay(argv[2], argv[3], argv[4], argv[5]);
      } else if (argc == 5 && std::string(argv[1]) == "--recover") {
        futari_replay::need(futari_replay::validContent(argv[3]) && futari_replay::validBuild(argv[4]),
                            "Invalid content or package build identity");
        futari_replay::Lock lock(argv[2]);
        futari_replay::recoverPendingImport(argv[2], argv[3], argv[4]);
      } else {
        throw std::runtime_error("Usage: --export USER_DATA EXPORT_DESTINATION CONTENT BUILD_SHA256 | --import ARCHIVE USER_DATA CONTENT BUILD_SHA256 | --recover USER_DATA CONTENT BUILD_SHA256");
      }
      return 0;
    } catch (const std::exception& e) {
      std::cerr << "Replay operation stopped safely: " << e.what() << "\n";
      return 1;
    }
  }
}
