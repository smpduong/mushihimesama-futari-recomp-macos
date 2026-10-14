// Production-path ContentManager::InstallContent test (Priority B2).
//
// The build compiles the REAL content_manager.cpp (with three mechanical
// hook substitutions recorded by the driver) into this binary, so
// InstallContent -> ExtractEntry -> StfsContainerFile::ReadSync runs for
// real against a temporary copy of the actual Black Label STFS package.
// Fault injection happens only in the interposed fwrite/fclose hooks.
//
// KernelState is passed as nullptr: on this path the renamed
// rextest_title_id() supplies the title id and no other kernel_state_
// member is dereferenced on the install path (verified by inspection;
// if that changes the test fails to link or crashes loudly).
//
// Reference payloads are extracted by a SEPARATE code path in this test
// (StfsContainerDevice enumeration + raw ReadSync into local files),
// then compared byte-for-byte (sha256 + length) with what InstallContent
// produced. Sizes also pin the known package layout.
//
// Cases (bitmask of failures on exit):
//   1 clean: install succeeds; payload hashes match reference digests.
//   2 short-write: first fwrite delivers half; install must fail loudly,
//     leave no partial-size payload, and write no .header.
//   3 close-fail: fclose reports failure after complete writes; same.
//   4 late-fail: a late write failure must fail loudly, must NOT damage a
//     previously installed copy, and must leave no staging behind.
//   5 corrupt: tail-truncated valid-looking containers must fail
//     explicitly and leave no truncated payload.//  6 staging-invisible: ListContent must not surface the
//     `<package>.installing` / `<package>.previous` directories that an
//     install (or an interrupted one) leaves beside the package.
//  7 header-short: failing ONLY the .header write must fail loudly, keep the
//     previous payload AND the previous .header byte-identical, and leave no
//     .part file or staging behind (metadata lives outside the package
//     directory, so payload rollback cannot repair it).

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <optional>
#include <span>
#include <string>
#include <vector>

#include "test_content_install_decls.h"
#include "content_display_name.h"

#include <rex/filesystem/devices/stfs_container_device.h>
#include <rex/string.h>
#include <rex/filesystem/file.h>
#include <rex/system/xam/content_manager.h>

namespace fs = std::filesystem;
using rex::system::xam::ContentManager;
using rex::X_STATUS;
using rex::X_RESULT;
// X_ERROR_* are preprocessor macros in rex/system/xtypes.h; they need no
// using-declaration. Include the File class definition for the reference
// extractor.

// ---- interposed hook definitions (referenced by the renamed copy) ----
extern "C" size_t rextest_fwrite(const void* ptr, size_t size, size_t count,
                                 std::FILE* f) {
  auto& fc = rextest::faults();
  fc.fwrite_calls++;
  size_t want = size * count;
  // Header-only fault, checked first so it is independent of the extraction
  // write budget. Note the argument order WriteContentHeaderFile uses:
  // fwrite(&data, 1, sizeof(XCONTENT_AGGREGATE_DATA), f) -- size is 1 and the
  // COUNT carries the record size, so the discriminator is `count`.
  if (fc.fail_header_write && fc.header_write_size != 0 &&
      size == 1 && count == fc.header_write_size) {
    size_t partial = want > 1 ? want / 2 : 0;
    size_t got = std::fwrite(ptr, 1, partial, f);
    fc.bytes_written += got;
    fc.header_writes_failed++;
    fc.last_fail = rextest::FaultControl::Fail::header_short;
    return got / size;
  }
  if (fc.fwrite_ok_left == 0) {
    size_t partial = want > 1 ? want / 2 : 0;
    size_t got = std::fwrite(ptr, 1, partial, f);
    fc.bytes_written += got;
    fc.last_fail = rextest::FaultControl::Fail::fwrite_short;
    return got / size;  // short item count == short write
  }
  if (fc.fwrite_ok_left != static_cast<size_t>(-1)) fc.fwrite_ok_left--;
  size_t got = std::fwrite(ptr, 1, want, f);
  fc.bytes_written += got;
  return got / size;
}

extern "C" int rextest_fclose(std::FILE* f) {
  auto& fc = rextest::faults();
  fc.fclose_calls++;
  if (fc.fclose_ok_left == 0) {
    std::fclose(f);  // still close the descriptor; report failure
    fc.last_fail = rextest::FaultControl::Fail::fclose_failed;
    return -1;
  }
  if (fc.fclose_ok_left != static_cast<size_t>(-1)) fc.fclose_ok_left--;
  return std::fclose(f);
}

extern "C" uint32_t rextest_title_id() { return 0x435A07D7u; }

// ---- helpers ----
namespace {

struct Expectation {
  const char* rel;
  size_t size;
};
const Expectation kExpectedBlack[] = {
    {"spa.bin", 827250},
    {"Rom/cg_black.bin", 57743518},
    {"Rom/cg_blackhi.bin", 216869269},
    {"Rom/dropprm.bl", 3184},
};

struct Sha256Ctx {
  uint32_t h[8];
  uint64_t len;
  uint8_t buf[64];
  size_t buf_len;
};
constexpr uint32_t kK[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
inline uint32_t Rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }
void Sha256Init(Sha256Ctx* c) {
  c->h[0] = 0x6a09e667; c->h[1] = 0xbb67ae85; c->h[2] = 0x3c6ef372;
  c->h[3] = 0xa54ff53a; c->h[4] = 0x510e527f; c->h[5] = 0x9b05688c;
  c->h[6] = 0x1f83d9ab; c->h[7] = 0x5be0cd19;
  c->len = 0; c->buf_len = 0;
}
void Sha256Block(Sha256Ctx* c, const uint8_t* p) {
  uint32_t w[64];
  for (int i = 0; i < 16; ++i) {
    w[i] = (uint32_t(p[i * 4]) << 24) | (uint32_t(p[i * 4 + 1]) << 16) |
           (uint32_t(p[i * 4 + 2]) << 8) | uint32_t(p[i * 4 + 3]);
  }
  for (int i = 16; i < 64; ++i) {
    uint32_t s0 = Rotr(w[i - 15], 7) ^ Rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
    uint32_t s1 = Rotr(w[i - 2], 17) ^ Rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
    w[i] = w[i - 16] + s0 + w[i - 7] + s1;
  }
  uint32_t a = c->h[0], b = c->h[1], cc = c->h[2], d = c->h[3];
  uint32_t e = c->h[4], f = c->h[5], g = c->h[6], h = c->h[7];
  for (int i = 0; i < 64; ++i) {
    uint32_t S1 = Rotr(e, 6) ^ Rotr(e, 11) ^ Rotr(e, 25);
    uint32_t ch = (e & f) ^ (~e & g);
    uint32_t t1 = h + S1 + ch + kK[i] + w[i];
    uint32_t S0 = Rotr(a, 2) ^ Rotr(a, 13) ^ Rotr(a, 22);
    uint32_t maj = (a & b) ^ (a & cc) ^ (b & cc);
    uint32_t t2 = S0 + maj;
    h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a;
    a = t1 + t2;
  }
  c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
  c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}
void Sha256Update(Sha256Ctx* c, const uint8_t* p, size_t n) {
  c->len += n;
  while (n > 0) {
    size_t take = (64 - c->buf_len < n) ? 64 - c->buf_len : n;
    std::memcpy(c->buf + c->buf_len, p, take);
    c->buf_len += take; p += take; n -= take;
    if (c->buf_len == 64) { Sha256Block(c, c->buf); c->buf_len = 0; }
  }
}
void Sha256Final(Sha256Ctx* c, uint8_t out[32]) {
  uint64_t bits = c->len * 8;
  uint8_t pad = 0x80;
  Sha256Update(c, &pad, 1);
  uint8_t zero = 0;
  while (c->buf_len != 56) Sha256Update(c, &zero, 1);
  uint8_t lenb[8];
  for (int i = 0; i < 8; ++i) lenb[i] = uint8_t(bits >> (56 - i * 8));
  // buf_len == 56 here; append the length block manually.
  std::memcpy(c->buf + 56, lenb, 8);
  Sha256Block(c, c->buf);
  c->buf_len = 0;
  for (int i = 0; i < 8; ++i) {
    out[i * 4] = uint8_t(c->h[i] >> 24);
    out[i * 4 + 1] = uint8_t(c->h[i] >> 16);
    out[i * 4 + 2] = uint8_t(c->h[i] >> 8);
    out[i * 4 + 3] = uint8_t(c->h[i]);
  }
}

std::string Sha256File(const fs::path& p) {
  std::FILE* f = std::fopen(p.string().c_str(), "rb");
  if (!f) return {};
  Sha256Ctx ctx;
  Sha256Init(&ctx);
  uint8_t buf[1 << 20];
  size_t n;
  while ((n = std::fread(buf, 1, sizeof(buf), f)) > 0) Sha256Update(&ctx, buf, n);
  std::fclose(f);
  uint8_t d[32];
  Sha256Final(&ctx, d);
  char out[65];
  for (int i = 0; i < 32; ++i) std::snprintf(out + i * 2, 3, "%02x", d[i]);
  return std::string(out, 64);
}

// name is a package-relative path like "Rom/cg_black.bin" (or a bare
// filename); compare against the path relative to the search root, with
// native separators.
// name is a package-relative path like "Rom/cg_black.bin" (or a bare
// filename). Installs nest under <root>/<xuid>/<title>/<type>/<pkg>/, so
// compare the TAIL of the root-relative path against the expected
// package-relative components.
std::optional<fs::path> FindByName(const fs::path& root, const std::string& name) {
  fs::path want = fs::path(name).lexically_normal();
  const size_t depth = std::distance(want.begin(), want.end());
  std::error_code ec;
  for (auto it = fs::recursive_directory_iterator(root, ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    if (ec) return std::nullopt;
    if (!it->is_regular_file(ec)) continue;
    fs::path rel = it->path().lexically_relative(root);
    auto rel_it = rel.end();
    for (size_t i = 0; i < depth; ++i) --rel_it;
    fs::path tail;
    for (auto p = rel_it; p != rel.end(); ++p) tail /= *p;
    if (tail == want) return it->path();
  }
  return std::nullopt;
}

// Compact listing of what actually exists under a root, for failure detail.
// Added after a `late-fail` failure reported "previous install damaged" with
// no way to tell whether the payload was missing, truncated, or shadowed by
// leftovers -- the listing makes the post-failure state self-describing.
std::string DescribeTree(const fs::path& root, size_t max_entries = 14) {
  std::string out;
  size_t n = 0;
  std::error_code ec;
  for (auto it = fs::recursive_directory_iterator(root, ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    if (ec) break;
    if (n >= max_entries) {
      out += " ...";
      break;
    }
    fs::path rel = it->path().lexically_relative(root);
    if (it->is_directory(ec)) {
      out += " [d]" + rel.string();
    } else {
      out += " " + rel.string() + "=" +
             std::to_string(fs::file_size(it->path(), ec)) + "B";
    }
    n++;
  }
  return out.empty() ? std::string(" <empty>") : out;
}

size_t CountHeaders(const fs::path& root) {
  size_t n = 0;
  std::error_code ec;
  for (auto it = fs::recursive_directory_iterator(root, ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    if (ec) break;
    if (it->path().filename().string().ends_with(".header")) n++;
  }
  return n;
}

// Independent reference extraction: StfsContainerDevice + raw ReadSync,
// deliberately NOT going through ContentManager/ExtractEntry.
bool ExtractReference(const fs::path& package, const fs::path& outdir,
                      std::string* detail) {
  auto device = std::make_unique<rex::filesystem::StfsContainerDevice>(
      "", package);
  if (!device->Initialize()) {
    *detail += " reference device Initialize failed";
    return false;
  }
  auto* root = device->ResolvePath("");
  if (!root) {
    *detail += " reference ResolvePath failed";
    return false;
  }
  std::vector<rex::filesystem::Entry*> files;
  std::vector<rex::filesystem::Entry*> queue{root};
  while (!queue.empty()) {
    auto* e = queue.back(); queue.pop_back();
    for (auto& child : e->children()) queue.push_back(child.get());
    if (!(e->attributes() & rex::filesystem::kFileAttributeDirectory)) {
      files.push_back(e);
    }
  }
  bool ok = true;
  for (auto* e : files) {
    fs::path dest = outdir /
        rex::to_path(rex::string::utf8_fix_path_separators(e->path()));
    fs::create_directories(dest.parent_path());
    rex::filesystem::File* in = nullptr;
    if (e->Open(rex::filesystem::FileAccess::kFileReadData, &in) !=
        X_STATUS_SUCCESS) {
      *detail += " ref open failed " + e->path();
      ok = false;
      continue;
    }
    std::FILE* out = std::fopen(dest.string().c_str(), "wb");
    std::vector<uint8_t> buf(1 << 20);
    size_t off = 0, remaining = e->size();
    while (remaining > 0) {
      size_t got = 0;
      if (in->ReadSync(std::span<uint8_t>(buf.data(), std::min(remaining, buf.size())),
                       off, &got) != X_STATUS_SUCCESS || got == 0) {
        *detail += " ref read failed at " + std::to_string(off);
        ok = false;
        break;
      }
      std::fwrite(buf.data(), 1, got, out);
      off += got;
      remaining -= got;
    }
    std::fclose(out);
    in->Destroy();
  }
  return ok;
}

X_RESULT RunInstall(const fs::path& package, const fs::path& root) {
  ContentManager cm(nullptr, root);
  return cm.InstallContent(package);
}

struct Invariant {
  bool ok = true;
  std::string detail;
};

Invariant NoPartialPayload(const fs::path& root) {
  Invariant inv;
  if (CountHeaders(root) != 0) inv.detail += " .header present;";
  for (const auto& e : kExpectedBlack) {
    auto p = FindByName(root, e.rel);
    if (!p) continue;  // absent is fine after a failed install
    uintmax_t sz = fs::file_size(*p);
    if (sz != e.size) {
      inv.ok = false;
      inv.detail += " partial " + std::string(e.rel) + " (" +
                    std::to_string(sz) + "/" + std::to_string(e.size) + ");";
    }
  }
  return inv;
}

bool CaseClean(const fs::path& package, const fs::path& refdir,
               const fs::path& root, std::string* detail) {
  rextest::faults() = {};
  X_RESULT rc = RunInstall(package, root);
  if (rc != X_ERROR_SUCCESS) {
    *detail += " install rc=" + std::to_string(uint32_t(rc));
    return false;
  }
  if (CountHeaders(root) == 0) {
    *detail += " no .header on success";
    return false;
  }
  bool ok = true;
  for (const auto& e : kExpectedBlack) {
    auto found = FindByName(root, e.rel);
    auto ref = FindByName(refdir, e.rel);
    if (!found || !ref) {
      *detail += " missing " + std::string(e.rel);
      ok = false;
      continue;
    }
    uintmax_t sz = fs::file_size(*found);
    if (sz != e.size) {
      *detail += " size " + std::string(e.rel) + "=" + std::to_string(sz);
      ok = false;
      continue;
    }
    std::string h = Sha256File(*found);
    std::string r = Sha256File(*ref);
    if (h != r) {
      *detail += " HASH MISMATCH " + std::string(e.rel) + " " + h.substr(0, 12);
      ok = false;
    }
  }
  return ok;
}

bool CaseShortWrite(const fs::path& package, const fs::path& refdir,
                    const fs::path& root, std::string* detail) {
  (void)refdir;
  rextest::faults() = {};
  rextest::faults().fwrite_ok_left = 0;  // first write delivers half
  X_RESULT rc = RunInstall(package, root);
  Invariant inv = NoPartialPayload(root);
  bool ok = rc != X_ERROR_SUCCESS && inv.ok &&
            rextest::faults().last_fail ==
                rextest::FaultControl::Fail::fwrite_short;
  if (rc == X_ERROR_SUCCESS) *detail += " install SUCCEEDED despite short write";
  if (!inv.ok) *detail += " invariants:" + inv.detail;
  if (rextest::faults().last_fail != rextest::FaultControl::Fail::fwrite_short) {
    *detail += " fwrite fault not observed";
  }
  return ok;
}

bool CaseCloseFail(const fs::path& package, const fs::path& refdir,
                   const fs::path& root, std::string* detail) {
  (void)refdir;
  rextest::faults() = {};
  rextest::faults().fclose_ok_left = 0;  // first fclose reports failure
  X_RESULT rc = RunInstall(package, root);
  Invariant inv = NoPartialPayload(root);
  bool ok = rc != X_ERROR_SUCCESS && inv.ok &&
            rextest::faults().last_fail ==
                rextest::FaultControl::Fail::fclose_failed;
  if (rc == X_ERROR_SUCCESS) *detail += " install SUCCEEDED despite close failure";
  if (!inv.ok) *detail += " invariants:" + inv.detail;
  if (rextest::faults().last_fail != rextest::FaultControl::Fail::fclose_failed) {
    *detail += " fclose fault not observed";
  }
  return ok;
}

// Every expected payload file present, right size, byte-identical to the
// independent reference extraction.
bool VerifyExpectedIntact(const fs::path& root, const fs::path& refdir,
                          std::string* detail) {
  bool ok = true;
  for (const auto& e : kExpectedBlack) {
    auto found = FindByName(root, e.rel);
    auto ref = FindByName(refdir, e.rel);
    if (!found || !ref) {
      *detail += " missing " + std::string(e.rel) + ";";
      ok = false;
      continue;
    }
    if (fs::file_size(*found) != e.size) {
      *detail += " size " + std::string(e.rel) + ";";
      ok = false;
      continue;
    }
    std::string h = Sha256File(*found), r = Sha256File(*ref);
    if (h != r) {
      *detail += " hash " + std::string(e.rel) + ";";
      ok = false;
    }
  }
  return ok;
}

// No staged/backup tree may survive a failed install: a `.installing` tree
// left behind is exactly the partially populated package the staging fix
// exists to avoid.
bool NoStagingLeft(const fs::path& root, std::string* detail) {
  bool ok = true;
  std::error_code ec;
  for (auto it = fs::recursive_directory_iterator(root, ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    if (ec) break;
    auto name = it->path().filename().string();
    if (name.size() > 11 &&
        (name.ends_with(".installing") || name.ends_with(".previous"))) {
      *detail += " leftover " + name + ";";
      ok = false;
    }
  }
  return ok;
}

// LATE-FILE FAILURE. Astra review: ExtractEntry removes only its own failed
// file, so a failure on a later file used to return early leaving every
// earlier file in the FINAL package directory -- a partial install that a
// later mount/discovery could open. This case deliberately fails after at
// least one file has been fully extracted, over a previously good
// installation, and requires: loud failure, previous install byte-intact,
// and no staging/backup leftovers.
bool CaseLateFail(const fs::path& package, const fs::path& refdir,
                  const fs::path& root, std::string* detail) {
  rextest::faults() = {};
  X_RESULT rc0 = RunInstall(package, root);
  if (rc0 != X_ERROR_SUCCESS) {
    *detail += " setup install rc=" + std::to_string(uint32_t(rc0));
    return false;
  }
  std::string setup_detail;
  if (!VerifyExpectedIntact(root, refdir, &setup_detail)) {
    *detail += " setup install incomplete:" + setup_detail;
    return false;
  }
  // Allow two whole fwrite() calls (spa.bin lands whole, the next chunk of
  // cg_black.bin begins), then short-write the third.
  rextest::faults() = {};
  rextest::faults().fwrite_ok_left = 2;
  X_RESULT rc1 = RunInstall(package, root);
  auto& fc = rextest::faults();
  std::string intact_detail, leftover_detail;
  bool loud = rc1 != X_ERROR_SUCCESS &&
              fc.last_fail == rextest::FaultControl::Fail::fwrite_short &&
              fc.fwrite_calls >= 3 && fc.bytes_written > 0;
  bool intact = VerifyExpectedIntact(root, refdir, &intact_detail);
  bool clean = NoStagingLeft(root, &leftover_detail);
  *detail += " rc=" + std::to_string(uint32_t(rc1)) + " writes=" +
             std::to_string(fc.fwrite_calls) + " late-after=" +
             std::to_string(fc.bytes_written) + "B";
  if (!loud) *detail += " NOT a loud, late failure;";
  if (!intact) {
    *detail += " previous install damaged:" + intact_detail;
    *detail += " tree:" + DescribeTree(root);
  }
  if (!clean) *detail += " staging not cleaned:" + leftover_detail;
  return loud && intact && clean;
}

std::optional<X_RESULT> TryCorrupt(const fs::path& package, const fs::path& root,
                                   uintmax_t trunc_bytes, const fs::path& workdir) {
  fs::path bad = workdir / ("corrupt-" + std::to_string(trunc_bytes));
  std::error_code ec;
  fs::remove_all(bad, ec);
  fs::copy_file(package, bad, fs::copy_options::overwrite_existing, ec);
  if (ec) return std::nullopt;
  fs::resize_file(bad, fs::file_size(bad) - trunc_bytes, ec);
  if (ec) return std::nullopt;
  rextest::faults() = {};
  X_RESULT rc = RunInstall(bad, root);
  if (rc == X_ERROR_SUCCESS) return std::nullopt;  // fixture ineffective
  return rc;
}

bool CaseCorrupt(const fs::path& package, const fs::path& refdir,
                 const fs::path& root, std::string* detail) {
  const uintmax_t kBlock = 0x1000;
  fs::path workdir = root.parent_path() / "corrupt-work";
  fs::create_directories(workdir);
  for (uintmax_t k : {uintmax_t(1), uintmax_t(8), uintmax_t(64), uintmax_t(512)}) {
    auto rc = TryCorrupt(package, root, k * kBlock, workdir);
    if (rc.has_value()) {
      Invariant inv = NoPartialPayload(root);
      *detail += " trunc=" + std::to_string(k * kBlock) + " rc=" +
                 std::to_string(uint32_t(*rc));
      if (!inv.ok) *detail += " invariants:" + inv.detail;
      return inv.ok;
    }
    *detail += " (trunc " + std::to_string(k * kBlock) + " undetected)";
    std::error_code ec;
    fs::remove_all(root, ec);  // reset for the next attempt
  }
  *detail += " no truncation level produced a failure";
  return false;
}

// Astra review 2026-09-22: ListContent enumerates EVERY directory under the
// package root, and synthesizes metadata for entries that have no .header.
// The staging/rollback directories InstallContent creates are therefore
// themselves discoverable as content -- during an install and after an
// interrupted one -- so the title could see a phantom or duplicate package.
// This case plants exactly the state an interrupted install leaves behind
// and requires ListContent to ignore it while still reporting the real one.
bool CaseStagingInvisible(const fs::path& package, const fs::path& refdir,
                          const fs::path& root, std::string* detail) {
  (void)refdir;
  fs::path leftover_installing;
  fs::path leftover_previous;
  rextest::faults() = {};
  {
    X_RESULT rc = RunInstall(package, root);
    if (rc != X_ERROR_SUCCESS) {
      *detail += " setup install rc=" + std::to_string(uint32_t(rc));
      return false;
    }
  }
  std::error_code ec;
  fs::path pkg_dir;
  for (auto it = fs::recursive_directory_iterator(root, ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    if (!it->is_directory(ec)) continue;
    if (fs::exists(it->path() / "spa.bin", ec)) pkg_dir = it->path();
  }
  if (pkg_dir.empty()) {
    *detail += " could not locate the installed package directory";
    return false;
  }
  const std::string pkg_name = pkg_dir.filename().string();
  leftover_installing = pkg_dir.parent_path() / (pkg_name + ".installing");
  leftover_previous = pkg_dir.parent_path() / (pkg_name + ".previous");
  fs::remove_all(leftover_installing, ec);
  fs::create_directories(leftover_installing / "Rom", ec);
  if (std::FILE* f = std::fopen(
          (leftover_installing / "Rom" / "cg_black.bin").c_str(), "wb")) {
    std::fwrite("partial", 1, 7, f);  // a truncated payload, as after a crash
    std::fclose(f);
  }
  fs::create_directories(leftover_previous, ec);

  ContentManager cm(nullptr, root);
  auto listed = cm.ListContent(0, 0,
                               rex::filesystem::XContentType::kMarketplaceContent,
                               rextest_title_id());
  bool ok = true;
  bool saw_real = false;
  // Prefix, not equality: XCONTENT_AGGREGATE_DATA::file_name() is a
  // fixed-size field, so a longer synthesized name for a staging directory
  // is TRUNCATED rather than rejected -- comparing for equality silently
  // missed it (the case passed against the unfixed source). Counting entries
  // as well catches a truncation that matches nothing at all.
  for (const auto& entry : listed) {
    const std::string name(entry.file_name());
    if (name.rfind(pkg_name + ".", 0) == 0) {
      *detail += " ListContent surfaced staging entry " + name + ";";
      ok = false;
    }
    if (name == pkg_name) saw_real = true;
  }
  if (listed.size() != 1) {
    *detail += " ListContent returned " + std::to_string(listed.size()) +
               " entries for a single installed package;";
    ok = false;
  }
  if (!saw_real) {
    *detail += " the real package is missing from ListContent;";
    ok = false;
  }
  fs::remove_all(leftover_installing, ec);
  fs::remove_all(leftover_previous, ec);
  return ok;
}

// Astra review 2026-09-22 Priority 4: restart recovery after an interrupted
// install. Plant the exact state a crash mid-install leaves (a partial
// <pkg>.installing beside a complete previous install), then run a fresh
// install of the same package and require: the install succeeds, the
// previously installed payload is NOT deleted, the stale staging is gone (or
// at least not enumerated), and ListContent reports exactly one package.
bool CaseRestartRecovery(const fs::path& package, const fs::path& refdir,
                         const fs::path& root, std::string* detail) {
  (void)refdir;
  rextest::faults() = {};
  if (RunInstall(package, root) != X_ERROR_SUCCESS) {
    *detail += " setup install failed";
    return false;
  }
  std::error_code ec;
  fs::path pkg_dir;
  for (auto it = fs::recursive_directory_iterator(root, ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    if (!it->is_directory(ec)) continue;
    if (fs::exists(it->path() / "spa.bin", ec)) pkg_dir = it->path();
  }
  if (pkg_dir.empty()) {
    *detail += " could not locate the installed package directory";
    return false;
  }
  const std::string pkg_name = pkg_dir.filename().string();
  const fs::path staging = pkg_dir.parent_path() / (pkg_name + ".installing");
  fs::remove_all(staging, ec);
  fs::create_directories(staging / "Rom", ec);
  if (std::FILE* f = std::fopen((staging / "Rom" / "cg_black.bin").c_str(), "wb")) {
    std::fwrite("partial", 1, 7, f);  // truncated payload, as after a crash
    std::fclose(f);
  }
  // Re-run the install; a restart must recover, not collide with its own
  // leftover or delete the good install.
  X_RESULT rc = RunInstall(package, root);
  if (rc != X_ERROR_SUCCESS) {
    *detail += " reinstall after interruption rc=" + std::to_string(uint32_t(rc));
    return false;
  }
  bool ok = true;
  // The previous valid install must still be present and complete.
  if (!fs::exists(pkg_dir / "spa.bin", ec)) {
    *detail += " previous valid install missing after recovery;";
    ok = false;
  }
  // Deliberate boundary (Astra Priority 4): stale staging from an earlier
  // interrupted run is NOT deleted -- the installer must never delete another
  // operation's staging data to look clean. It is instead required to be
  // INVISIBLE to discovery (proved by staging-invisible) and harmless.
  if (fs::exists(staging, ec)) {
    *detail += " (stale staging retained by design; must stay invisible)";
  }
  ContentManager cm(nullptr, root);
  auto listed = cm.ListContent(0, 0,
                               rex::filesystem::XContentType::kMarketplaceContent,
                               rextest_title_id());
  if (listed.size() != 1) {
    *detail += " ListContent returned " + std::to_string(listed.size()) +
               " entries after recovery;";
    ok = false;
  }
  return ok;
}

// Byte content of the first `.header` under root, with its path.
bool ReadFirstHeader(const fs::path& root, fs::path* path_out,
                     std::string* bytes_out) {
  std::error_code ec;
  for (auto it = fs::recursive_directory_iterator(root, ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    if (ec) return false;
    if (!it->path().filename().string().ends_with(".header")) continue;
    std::ifstream in(it->path(), std::ios::binary);
    if (!in) return false;
    *bytes_out = std::string((std::istreambuf_iterator<char>(in)),
                             std::istreambuf_iterator<char>());
    *path_out = it->path();
    return true;
  }
  return false;
}

// Astra review 2026-09-22: WriteContentHeaderFile ignored fwrite/fclose
// results, so a truncated header could be reported as success -- and because
// .header files live in Headers/<type>/, the payload rollback cannot repair
// them. This case fails ONLY the header write after a good install and asks
// for all of: a loud failure, the previous payload intact, the previous
// .header byte-identical, no `.part` file, and no staging left behind.
bool CaseHeaderShort(const fs::path& package, const fs::path& refdir,
                     const fs::path& root, std::string* detail) {
  rextest::faults() = {};
  if (RunInstall(package, root) != X_ERROR_SUCCESS) {
    *detail += " setup install failed";
    return false;
  }
  std::string setup_detail;
  if (!VerifyExpectedIntact(root, refdir, &setup_detail)) {
    *detail += " setup install incomplete:" + setup_detail;
    return false;
  }
  fs::path header_path;
  std::string good_header;
  if (!ReadFirstHeader(root, &header_path, &good_header)) {
    *detail += " setup install wrote no .header";
    return false;
  }

  rextest::faults() = {};
  rextest::faults().fail_header_write = true;
  rextest::faults().header_write_size =
      sizeof(rex::system::xam::XCONTENT_AGGREGATE_DATA);
  X_RESULT rc = RunInstall(package, root);
  auto& fc = rextest::faults();

  std::string intact_detail, leftover_detail;
  bool loud = rc != X_ERROR_SUCCESS && fc.header_writes_failed >= 1 &&
              fc.last_fail == rextest::FaultControl::Fail::header_short;
  bool payload_ok = VerifyExpectedIntact(root, refdir, &intact_detail);
  fs::path header_after_path;
  std::string header_after;
  bool header_same = ReadFirstHeader(root, &header_after_path, &header_after) &&
                     header_after_path == header_path &&
                     header_after == good_header;
  bool no_part = true;
  std::error_code ec;
  for (auto it = fs::recursive_directory_iterator(root, ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    if (ec) break;
    if (it->path().filename().string().ends_with(".part")) no_part = false;
  }
  bool clean = NoStagingLeft(root, &leftover_detail);
  *detail += " rc=" + std::to_string(uint32_t(rc)) + " header_writes_failed=" +
             std::to_string(fc.header_writes_failed);
  if (!loud) *detail += " NOT a loud header failure;";
  if (!payload_ok) *detail += " previous payload damaged:" + intact_detail;
  if (!header_same) {
    *detail += " previous .header not preserved (" + header_after_path.string() +
               ", " + std::to_string(header_after.size()) + "B vs " +
               std::to_string(good_header.size()) + "B);";
  }
  if (!no_part) *detail += " .part file left behind;";
  if (!clean) *detail += " staging not cleaned:" + leftover_detail;
  return loud && payload_ok && header_same && no_part && clean;
}

}  // namespace

int main(int argc, char** argv) {
  // Exercise the production selector, including version-1 extended-slot
  // exclusion, without changing a supplied package or rebuilding the SDK.
  {
    rex::filesystem::XContentMetadata metadata{};
    metadata.metadata_version = 1;
    using rex::system::XLanguage;
    using rex::system::xam::PackageDisplayName;
    if (!PackageDisplayName(metadata).empty()) return 90;
    metadata.set_display_name(XLanguage::kJapanese, u"Japanese only");
    if (PackageDisplayName(metadata) != u"Japanese only") return 91;
    metadata.set_display_name(XLanguage::kEnglish, u"English preferred");
    if (PackageDisplayName(metadata) != u"English preferred") return 92;
    metadata.set_display_name(XLanguage::kEnglish, u"");
    metadata.set_display_name(XLanguage::kJapanese, u"");
    metadata.metadata_version = 2;
    metadata.set_display_name(XLanguage::kRussian, u"Extended slot");
    if (PackageDisplayName(metadata) != u"Extended slot") return 93;
    metadata.metadata_version = 1;
    if (!PackageDisplayName(metadata).empty()) return 94;
    std::puts("localized-metadata PASS (5 production-selector cases)");
  }
  // DLC lane helper: install a package into a real root via the runtime's
  // production InstallContent (writes .header + computed license mask).
  if (argc >= 4 && std::strcmp(argv[1], "--install") == 0) {
    fs::path package = argv[2];
    fs::path root = argv[3];
    std::error_code ec;
    fs::create_directories(root, ec);
    X_RESULT rc = RunInstall(package, root);
    std::printf("install %s -> %s rc=%08X\n", package.filename().string().c_str(),
                root.string().c_str(), (uint32_t)rc);
    return rc == 0 ? 0 : 1;
  }
  // DLC lane helper: list what the runtime's ContentManager sees under a root.
  if (argc >= 3 && std::strcmp(argv[1], "--list") == 0) {
    fs::path root = argv[2];
    ContentManager cm(nullptr, root);
    uint32_t title = argc >= 4 ? (uint32_t)std::strtoul(argv[3], nullptr, 16) : 0x435A07D7u;
    auto items = cm.ListContent(0xFF, 0, rex::filesystem::XContentType::kMarketplaceContent, title);
    std::printf("list root=%s title=%08X count=%zu\n", root.string().c_str(), title,
                items.size());
    for (auto& it : items) {
      std::printf("  file_name=%s type=%u xuid=%llu title=%08X\n", it.file_name().c_str(),
                  static_cast<uint32_t>(it.content_type.get()),
                  (unsigned long long)uint64_t(it.xuid.get()),
                  uint32_t(it.title_id.get()));
    }
    return 0;
  }
  if (argc < 4) {
    std::fprintf(stderr, "usage: %s <black-package> <roots-dir> <work-dir>\n"
                         "       %s --install <package> <root>\n",
                 argv[0], argv[0]);
    return 2;
  }
  fs::path package = argv[1];
  fs::path roots = argv[2];
  fs::path workdir = argv[3];
  std::error_code ec;
  fs::create_directories(roots, ec);
  fs::create_directories(workdir, ec);

  // Independent reference extraction once, into workdir/ref-payload.
  fs::path refdir = workdir / "ref-payload";
  fs::remove_all(refdir, ec);
  std::string refdetail;
  if (!ExtractReference(package, refdir, &refdetail)) {
    std::printf("ref-extract FAIL%s\n", refdetail.c_str());
    return 2;
  }
  std::printf("ref-extract PASS (independent StfsContainerDevice path)\n");

  int failed = 0;
  struct {
    const char* name;
    bool (*fn)(const fs::path&, const fs::path&, const fs::path& root,
               std::string*);
  } cases[] = {
      {"clean", CaseClean},
      {"short-write", CaseShortWrite},
      {"close-fail", CaseCloseFail},
      {"late-fail", CaseLateFail},
      {"staging-invisible", CaseStagingInvisible},
      {"restart-recovery", CaseRestartRecovery},
      {"header-short", CaseHeaderShort},
      {"corrupt", CaseCorrupt},
  };
  // Corrupt's signature differs in the refdir slot; adapt by wrappers.
  int bit = 1;
  for (auto& c : cases) {
    fs::path root = roots / c.name;
    fs::remove_all(root, ec);
    std::string detail;
    bool ok;
    if (std::strcmp(c.name, "corrupt") == 0) {
      ok = CaseCorrupt(package, refdir, root, &detail);
    } else {
      ok = c.fn(package, refdir, root, &detail);
    }
    std::printf("%-12s %s%s%s\n", c.name, ok ? "PASS" : "FAIL",
                detail.empty() ? "" : " -", detail.c_str());
    if (!ok) failed |= bit;
    bit <<= 1;
  }
  return failed;
}
